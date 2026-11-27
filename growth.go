package main

// 成长值（积分）自动获取：
//  1. 只读查询 POST /v2/billing/meter/checkin-status（客户端成长计划页同一接口），
//     复用每日签到的凭据/请求头管线，把「待领取/已领取/连签天数/宝箱日」写进展示快照。
//  2. 自动补领仅当「自动签到」开关开启、今日计划可补签（与手动补签同一准入）且上游
//     确认今日未领取时，走既有 claimManualCheckin→runJob 通道，不新增第二套定时器。

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

var errGrowthUnsupported = errors.New("仅 CN 站账号有成长值")

type growthSnapshot struct {
	Active      bool    `json:"active"`
	CheckedIn   bool    `json:"checkedIn"`
	TodayCredit float64 `json:"todayCredit,omitempty"`
	StreakDays  int     `json:"streakDays,omitempty"`
	IsStreakDay bool    `json:"isStreakDay,omitempty"`
	UpdatedAt   int64   `json:"updatedAt,omitempty"`
}

// 上游响应为 snake_case（active/today_checked_in/today_credit/streak_days/is_streak_day），
// 与客户端 useCheckinService 消费的字段一致；数值字段容忍字符串形式。
type flexNum float64

func (f *flexNum) UnmarshalJSON(b []byte) error {
	s := strings.Trim(string(b), `"`)
	if s == "" || s == "null" {
		*f = 0
		return nil
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		v = 0
	}
	*f = flexNum(v)
	return nil
}

type upstreamGrowthStatus struct {
	Active      bool    `json:"active"`
	CheckedIn   bool    `json:"today_checked_in"`
	TodayCredit flexNum `json:"today_credit"`
	StreakDays  flexNum `json:"streak_days"`
	IsStreakDay bool    `json:"is_streak_day"`
}

func (u upstreamGrowthStatus) toSnapshot() *growthSnapshot {
	return &growthSnapshot{
		Active:      u.Active,
		CheckedIn:   u.CheckedIn,
		TodayCredit: float64(u.TodayCredit),
		StreakDays:  int(u.StreakDays),
		IsStreakDay: u.IsStreakDay,
	}
}

func (p *upstreamProfile) checkinStatusURL() string {
	return p.Origin + "/v2/billing/meter/checkin-status"
}

var (
	growthMu       sync.Mutex
	growthCache    = map[string]growthSnapshot{}
	growthLastTry  = map[string]time.Time{}
	growthLastPlan = map[string]string{}
)

// growthView 返回缓存副本；没有数据时返回 nil，展示层留空即可。
func growthView(key string) *growthSnapshot {
	if key == "" {
		return nil
	}
	growthMu.Lock()
	defer growthMu.Unlock()
	if g, ok := growthCache[key]; ok {
		return &g
	}
	return nil
}

// fetchGrowthStatus 与 checkinAccountWithTrace 同一请求管线：账号读锁、通用头、
// Bearer 凭据、平台/用户标识。只读查询，单次发送，不做网络重试。
func fetchGrowthStatus(ctx context.Context, acc *Account, trace string) (*growthSnapshot, error) {
	ctx, cancel := context.WithTimeout(ctx, 12*time.Second)
	defer cancel()
	if !tryAccountLockWithContext(ctx, acc.lock.TryRLock) {
		return nil, ctx.Err()
	}
	accountMu.Lock()
	var auth StoredAuth
	prof := *acc.Profile()
	if acc.Auth != nil {
		auth = *acc.Auth
	}
	accountMu.Unlock()
	acc.lock.RUnlock() // 网络 IO 不持有模型凭据锁
	if prof.Key != profileCN.Key {
		return nil, errGrowthUnsupported
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, prof.checkinStatusURL(), strings.NewReader("{}"))
	if err != nil {
		return nil, err
	}
	commonHeaders(req, &prof)
	req.Header.Set("Authorization", "Bearer "+auth.Auth.AccessToken)
	req.Header.Set("X-Client-Platform", "web")
	if auth.Account.UID != "" {
		req.Header.Set("X-User-Id", auth.Account.UID)
	}
	if auth.Auth.Domain != "" {
		req.Header.Set("X-Domain", auth.Auth.Domain)
	}
	client := *cfg.HttpClient
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	log.Printf("[成长值] traceId=%s 阶段=请求上游 方法=POST 说明=只读查询签到状态，不记录凭据或响应原文", trace)
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
	var envelope struct {
		Code *int                 `json:"code"`
		Msg  string               `json:"msg"`
		Data upstreamGrowthStatus `json:"data"`
	}
	parseErr := json.Unmarshal(raw, &envelope)
	if readErr != nil || parseErr != nil || resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, errors.New("响应解析失败")
	}
	if envelope.Code == nil || *envelope.Code != 0 {
		log.Printf("[成长值] traceId=%s 阶段=响应校验 HTTP=%d 说明=业务码非 0，不更新缓存", trace, resp.StatusCode)
		return nil, errors.New("上游返回业务错误")
	}
	return envelope.Data.toSnapshot(), nil
}

const (
	growthRefreshInterval = 10 * time.Minute
	growthMinAttemptGap   = 3 * time.Minute
)

// startGrowthMonitor 周期刷新成长值缓存并在满足条件时自动补领；
// 返回清理函数，与 startManualCheckinServer 相同的挂载方式。
func startGrowthMonitor(ctx context.Context) func() {
	done := make(chan struct{})
	go func() {
		timer := time.NewTimer(45 * time.Second)
		defer timer.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-done:
				return
			case <-timer.C:
			}
			runGrowthCycle(ctx)
			timer.Reset(growthRefreshInterval)
		}
	}()
	return func() { close(done) }
}

func runGrowthCycle(ctx context.Context) {
	targets := currentCheckinTargets()
	now := time.Now()
	var claims []checkinTarget
	for _, t := range targets {
		if ctx.Err() != nil || !t.supported || t.account == nil || !t.eligible {
			continue
		}
		snap := dailyCheckins.view(t, now)
		growthMu.Lock()
		lastPlan := growthLastPlan[t.key]
		lastTry := growthLastTry[t.key]
		growthMu.Unlock()
		planChanged := snap.Status == "success" && lastPlan != "success"
		due := lastTry.IsZero() || time.Since(lastTry) >= growthRefreshInterval ||
			(planChanged && time.Since(lastTry) >= growthMinAttemptGap)
		if !due {
			continue
		}
		trace := newTraceID()
		g, err := fetchGrowthStatus(ctx, t.account, trace)
		growthMu.Lock()
		growthLastTry[t.key] = now
		growthLastPlan[t.key] = snap.Status
		if g != nil {
			g.UpdatedAt = now.Unix()
			growthCache[t.key] = *g
		}
		growthMu.Unlock()
		if err != nil {
			log.Printf("[成长值] traceId=%s 阶段=查询上游 结果=失败 类型=%s 说明=单次发送不重试，不影响签到计划", trace, debugErrorType(err))
			continue
		}
		log.Printf("[成长值] traceId=%s 阶段=查询上游 结果=成功 今日已领=%t 待领积分=%.0f 连签=%d 天 宝箱日=%t",
			trace, g.CheckedIn, g.TodayCredit, g.StreakDays, g.IsStreakDay)
		// 自动补领仅在计划已过期（随机签到时间已过仍无尝试，或已判 missed）且上游确认
		// 未领取时触发；不在计划时间之前抢先签到，保持上游 08:00–09:30 随机化设计。
		dueForCatchup := snap.ManualReady &&
			((snap.Status == "pending" && snap.NextAt > 0 && now.Unix() >= snap.NextAt) || snap.Status == "missed")
		if automaticCheckinEnabled() && g.Active && !g.CheckedIn && dueForCatchup {
			claims = append(claims, t)
		}
	}
	for i, t := range claims {
		if ctx.Err() != nil {
			return
		}
		if i > 0 {
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Minute): // 与启动补签一致：补领之间保持一分钟间隔
			}
		}
		job, code := dailyCheckins.claimManualCheckin(t)
		if code != http.StatusOK {
			log.Printf("[成长值] 阶段=自动补领 结果=跳过 账号标识=%s 说明=计划状态已变化，走正常调度", t.key[:12])
			continue
		}
		trace := job.trace
		log.Printf("[成长值] traceId=%s 阶段=自动补领 结果=已受理 账号标识=%s 说明=与手动补签同一通道", trace, t.key[:12])
		go dailyCheckins.runJob(ctx, job)
	}
}
