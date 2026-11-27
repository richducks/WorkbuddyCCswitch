package main

import (
	"context"
	"encoding/json"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"time"
)

var manualCredentialName = regexp.MustCompile(`^workbuddy[0-9]*\.json$`)

func automaticCheckinEnabled() bool {
	path := os.Getenv("WB_AUTO_CHECKIN_DISABLED_FILE")
	if path == "" {
		return true
	}
	_, err := os.Lstat(path)
	return os.IsNotExist(err)
}

// The old global setting remains the default for accounts without an override.
func accountAutomaticCheckinEnabled(target checkinTarget) bool {
	if target.account != nil && target.account.Path != "" {
		path := target.account.Path + ".auto-checkin"
		info, err := os.Lstat(path)
		if !os.IsNotExist(err) {
			if err != nil || !info.Mode().IsRegular() || info.Size() != int64(len("enabled\n")) {
				return false
			}
			data, err := os.ReadFile(path)
			return err == nil && string(data) == "enabled\n"
		}
	}
	return automaticCheckinEnabled()
}

func manualCheckinReady(p *checkinRecord, target checkinTarget, now time.Time) bool {
	start, _, _ := checkinWindows(now)
	return target.eligible && p != nil && (p.Status == "pending" || p.Status == "missed") &&
		p.Attempts == 0 && p.LastAt == 0 && (p.StartupAt == 0 || !accountAutomaticCheckinEnabled(target)) &&
		now.Before(start.Add(16*time.Hour-40*time.Second))
}

// Use the same lock, attempt journal and upstream worker as automatic check-in.
func (s *dailyCheckinScheduler) claimManualCheckin(target checkinTarget) (checkinJob, int) {
	return s.claimCheckin(target, "")
}

func (s *dailyCheckinScheduler) claimCheckin(target checkinTarget, observedDay string) (checkinJob, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.loadLocked()
	now := s.now()
	date := checkinDay(now)
	if s.blocked {
		return checkinJob{}, http.StatusServiceUnavailable
	}
	p := s.doc.Plans[target.key]
	if s.doc.Date != date || !manualCheckinReady(p, target, now) {
		return checkinJob{}, http.StatusConflict
	}
	automatic := observedDay != ""
	if automatic && (observedDay != date || !accountAutomaticCheckinEnabled(target) ||
		(p.Status == "pending" && p.FirstAt > now.UnixMilli())) {
		return checkinJob{}, http.StatusConflict
	}
	trace := newTraceID()
	p.Attempts, p.Status, p.LastAt = 1, "running", now.UnixMilli()
	p.StartupAt = 0
	p.Message = "正在手动签到"
	if automatic {
		p.Message = "正在自动补领"
	}
	if !s.saveLocked(trace) {
		return checkinJob{}, http.StatusServiceUnavailable
	}
	return checkinJob{key: target.key, date: date, attempt: 1, end: now.Add(40 * time.Second),
		account: target.account, trace: trace, started: now}, http.StatusOK
}

func findManualCheckinTarget(name string) (checkinTarget, bool) {
	accountMu.Lock()
	defer accountMu.Unlock()
	var found checkinTarget
	for _, acc := range accounts {
		if acc != nil && filepath.Base(acc.Path) == name {
			if found.account != nil {
				return checkinTarget{}, false
			}
			found = checkinTargetLocked(acc)
		}
	}
	return found, found.account != nil
}

func manualCheckinHandler(ctx context.Context, s *dailyCheckinScheduler, find func(string) (checkinTarget, bool)) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /checkin", func(w http.ResponseWriter, r *http.Request) {
		name := r.URL.Query().Get("file")
		if !manualCredentialName.MatchString(name) {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		target, ok := find(name)
		if !ok || ctx.Err() != nil {
			w.WriteHeader(http.StatusConflict)
			return
		}
		job, code := s.claimManualCheckin(target)
		if code != http.StatusOK {
			w.WriteHeader(code)
			return
		}
		// A disconnected browser must not interrupt an already claimed attempt.
		s.runJob(ctx, job)
		status := "unknown"
		s.mu.Lock()
		if p := s.doc.Plans[job.key]; !s.blocked && s.doc.Date == job.date && p != nil && p.Attempts == job.attempt {
			if p.Status == "success" || p.Status == "failed" {
				status = p.Status
			}
		}
		s.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": status})
	})
	return mux
}

// Enabled only by the Docker supervisor, on a socket in its private data dir.
func startManualCheckinServer(ctx context.Context, s *dailyCheckinScheduler) func() {
	path := os.Getenv("WB_MANUAL_CHECKIN_SOCKET")
	if path == "" {
		return func() {}
	}
	fail := func() func() {
		log.Print("[手动签到] 无法启动本地签到接口，自动签到不受影响")
		return func() {}
	}
	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return fail()
		}
		if conn, err := net.DialTimeout("unix", path, time.Second); err == nil {
			conn.Close()
			return fail()
		}
		if os.Remove(path) != nil {
			return fail()
		}
	} else if !os.IsNotExist(err) {
		return fail()
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		return fail()
	}
	if os.Chmod(path, 0600) != nil {
		listener.Close()
		return fail()
	}
	server := &http.Server{Handler: manualCheckinHandler(ctx, s, findManualCheckinTarget),
		ReadHeaderTimeout: 2 * time.Second, IdleTimeout: 5 * time.Second, MaxHeaderBytes: 4096}
	go server.Serve(listener)
	return func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if server.Shutdown(shutdownCtx) != nil {
			_ = server.Close()
		}
	}
}
