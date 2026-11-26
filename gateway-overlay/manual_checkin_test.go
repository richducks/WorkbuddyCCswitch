package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestManualCheckinUsesExistingScheduler(t *testing.T) {
	t.Chdir(t.TempDir())
	now := time.Date(2026, 1, 2, 8, 15, 0, 0, checkinZone)
	start, retry, _ := checkinWindows(now)
	for _, tc := range []struct {
		name, state, result, want string
		attempts                  int
		startup                   bool
		performErr                error
	}{
		{name: "pending", state: "pending", result: "ok", want: "success"},
		{name: "already upstream", state: "pending", result: "already", want: "success"},
		{name: "failure", state: "pending", want: "failed", performErr: errors.New("offline")},
		{name: "already local", state: "success", attempts: 1},
		{name: "missed can be signed without restart", state: "missed", result: "ok", want: "success"},
		{name: "startup already scheduled", state: "pending", startup: true},
		{name: "running", state: "running", attempts: 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := newDailyCheckinScheduler(filepath.Join(t.TempDir(), "checkin-state.json"))
			s.now = func() time.Time { return now }
			target := checkinTarget{key: strings.Repeat("a", 64), account: &Account{}, supported: true, eligible: true}
			other := strings.Repeat("b", 64)
			s.loaded = true
			p := &checkinRecord{Status: tc.state, Attempts: tc.attempts, FirstAt: start.Add(time.Hour).UnixMilli(), RetryAt: retry.UnixMilli()}
			if tc.startup {
				p.StartupAt = now.UnixMilli()
			}
			otherPlan := checkinRecord{Status: "pending", FirstAt: p.FirstAt, RetryAt: p.RetryAt}
			s.doc = checkinDocument{Schema: 1, Date: checkinDay(now), Plans: map[string]*checkinRecord{target.key: p, other: &otherPlan}}
			calls := 0
			s.perform = func(ctx context.Context, account *Account, _ string) (string, error) {
				calls++
				b, err := os.ReadFile(s.file)
				var saved checkinDocument
				if err != nil || json.Unmarshal(b, &saved) != nil || saved.Plans[target.key].Status != "running" || saved.Plans[target.key].Attempts != 1 {
					t.Fatal("attempt not persisted before upstream")
				}
				if account != target.account || ctx.Value(checkinExpectedIdentity{}) != target.key {
					t.Fatal("wrong target")
				}
				return tc.result, tc.performErr
			}
			if got := s.view(target, now).ManualReady; got != (tc.want != "") {
				t.Fatalf("manual readiness = %v", got)
			}
			h := manualCheckinHandler(context.Background(), s, func(string) (checkinTarget, bool) { return target, true })
			post := func() *httptest.ResponseRecorder {
				r := httptest.NewRequest(http.MethodPost, "/checkin?file=workbuddy2.json", nil)
				w := httptest.NewRecorder()
				h.ServeHTTP(w, r)
				return w
			}
			w := post()
			if tc.want == "" {
				if w.Code != http.StatusConflict || calls != 0 {
					t.Fatalf("ineligible request executed: %d, %d calls", w.Code, calls)
				}
				return
			}
			var result struct{ Status string }
			if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &result) != nil || result.Status != tc.want || p.Status != tc.want {
				t.Fatalf("wrong result: %d, %s, %s", w.Code, w.Body, p.Status)
			}
			if s.doc.Plans[other].Status != "pending" || s.doc.Plans[other].Attempts != 0 {
				t.Fatal("other account changed")
			}
			if post().Code != http.StatusConflict || calls != 1 {
				t.Fatal("duplicate went upstream")
			}
		})
	}
}

func TestCheckinMaintenanceRestartKeepsFutureSchedule(t *testing.T) {
	t.Setenv("WB_PRESERVE_CHECKIN_SCHEDULE", "1")
	now := time.Date(2026, 1, 2, 8, 15, 0, 0, checkinZone)
	_, retry, _ := checkinWindows(now)
	s := newDailyCheckinScheduler(filepath.Join(t.TempDir(), "checkin-state.json"))
	first := now.Add(time.Hour).UnixMilli()
	p := &checkinRecord{Status: "pending", FirstAt: first, RetryAt: retry.UnixMilli()}
	target := checkinTarget{key: strings.Repeat("a", 64), eligible: true}
	s.loaded = true
	s.doc = checkinDocument{Schema: 1, Date: checkinDay(now), Plans: map[string]*checkinRecord{target.key: p}}
	if got := s.planAndClaim(now, []checkinTarget{target}, true); len(got) != 0 || p.FirstAt != first || p.StartupAt != 0 {
		t.Fatal("maintenance restart advanced a pending automatic check-in")
	}
}

func TestCheckinAutomaticCatchupRejectsStaleDayAndFuturePlan(t *testing.T) {
	now := time.Date(2026, 1, 2, 8, 15, 0, 0, checkinZone)
	_, retry, _ := checkinWindows(now)
	s := newDailyCheckinScheduler(filepath.Join(t.TempDir(), "checkin-state.json"))
	s.now = func() time.Time { return now }
	s.loaded = true
	target := checkinTarget{key: strings.Repeat("a", 64), eligible: true}
	plan := &checkinRecord{Status: "pending", FirstAt: now.Add(time.Minute).UnixMilli(), RetryAt: retry.UnixMilli()}
	s.doc = checkinDocument{Schema: 1, Date: checkinDay(now), Plans: map[string]*checkinRecord{target.key: plan}}
	for _, day := range []string{"2026-01-01", checkinDay(now)} {
		if _, code := s.claimCheckin(target, day); code != http.StatusConflict || plan.Attempts != 0 {
			t.Fatalf("未到时间或隔日的自动补领不应占用签到尝试: day=%s code=%d", day, code)
		}
	}
	now = now.Add(2 * time.Minute)
	if _, code := s.claimCheckin(target, checkinDay(now)); code != http.StatusOK || plan.Attempts != 1 {
		t.Fatalf("同日到点应允许补领: %d", code)
	}
}

func TestGrowthStatusRejectsNonFiniteCredit(t *testing.T) {
	for _, v := range []string{`"NaN"`, `"Inf"`, `-1`} {
		var value upstreamGrowthStatus
		if json.Unmarshal([]byte(`{"active":true,"today_credit":`+v+`}`), &value) == nil {
			t.Fatalf("无效积分值不能写入状态快照: %s", v)
		}
	}
}

func TestCheckinAutoSwitchGatesAttemptsButNotManual(t *testing.T) {
	now := time.Date(2026, 1, 2, 8, 15, 0, 0, checkinZone)
	_, retry, _ := checkinWindows(now)
	dir := t.TempDir()
	flag := filepath.Join(dir, "auto-checkin.disabled")
	t.Setenv("WB_AUTO_CHECKIN_DISABLED_FILE", flag)
	if err := os.WriteFile(flag, nil, 0600); err != nil {
		t.Fatal(err)
	}
	s := newDailyCheckinScheduler(filepath.Join(dir, "checkin-state.json"))
	s.now = func() time.Time { return now }
	a := checkinTarget{key: strings.Repeat("a", 64), eligible: true, supported: true}
	b := checkinTarget{key: strings.Repeat("b", 64), eligible: true, supported: true}
	plan := checkinRecord{Status: "pending", FirstAt: now.UnixMilli(), RetryAt: retry.UnixMilli()}
	pa, pb := plan, plan
	s.loaded = true
	s.doc = checkinDocument{Schema: 1, Date: checkinDay(now), Plans: map[string]*checkinRecord{a.key: &pa, b.key: &pb}}
	for _, startup := range []bool{true, false} {
		if got := s.planAndClaim(now, []checkinTarget{a, b}, startup); len(got) != 0 || pa.Attempts != 0 || pb.Attempts != 0 {
			t.Fatal("disabled automatic check-in still reserved attempts")
		}
	}
	if _, code := s.claimManualCheckin(a); code != http.StatusOK {
		t.Fatalf("manual check-in blocked by switch: %d", code)
	}
	if err := os.Remove(flag); err != nil {
		t.Fatal(err)
	}
	if got := s.planAndClaim(now, []checkinTarget{a, b}, false); len(got) != 1 || got[0].key != b.key || pa.Attempts != 1 {
		t.Fatal("enabling did not restore automatic scheduling without duplicating a manual claim")
	}
}

func TestCheckinPerAccountSwitch(t *testing.T) {
	t.Chdir(t.TempDir())
	t.Setenv("WB_AUTO_CHECKIN_DISABLED_FILE", "auto-checkin.disabled")
	now := time.Date(2026, 1, 2, 8, 15, 0, 0, checkinZone)
	_, retry, _ := checkinWindows(now)
	a := checkinTarget{key: strings.Repeat("a", 64), eligible: true, account: &Account{Path: "workbuddy.json"}}
	b := checkinTarget{key: strings.Repeat("b", 64), eligible: true, account: &Account{Path: "workbuddy2.json"}}
	write := func(path, value string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
	}
	for _, startup := range []bool{false, true} {
		write("workbuddy.json.auto-checkin", "disabled\n")
		s := newDailyCheckinScheduler(filepath.Join(t.TempDir(), "checkin-state.json"))
		s.now = func() time.Time { return now }
		plan := checkinRecord{Status: "pending", FirstAt: now.UnixMilli(), RetryAt: retry.UnixMilli()}
		pa, pb := plan, plan
		s.loaded = true
		s.doc = checkinDocument{Schema: 1, Date: checkinDay(now), Plans: map[string]*checkinRecord{a.key: &pa, b.key: &pb}}
		if jobs := s.planAndClaim(now, []checkinTarget{a, b}, startup); len(jobs) != 1 || jobs[0].key != b.key || pa.Attempts != 0 {
			t.Fatalf("startup=%v: expected only the enabled account to start", startup)
		}
		if _, code := s.claimCheckin(a, checkinDay(now)); code != http.StatusConflict || pa.Attempts != 0 {
			t.Fatalf("disabled account still claimed for automatic catch-up: %d", code)
		}
		if _, code := s.claimManualCheckin(a); code != http.StatusOK {
			t.Fatalf("disabled auto check-in blocked manual check-in: %d", code)
		}
	}
	write("auto-checkin.disabled", "")
	write("workbuddy.json.auto-checkin", "enabled\n")
	if !accountAutomaticCheckinEnabled(a) || accountAutomaticCheckinEnabled(b) {
		t.Fatal("per-account enable did not override the legacy default")
	}
}
