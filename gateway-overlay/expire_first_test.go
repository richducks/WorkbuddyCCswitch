package main

// expire-first 调度策略单元测试（本地 overlay）。

import (
	"encoding/json"
	"strconv"
	"testing"
	"time"
)

func withExpireFirst(t *testing.T, enabled bool) {
	t.Helper()
	prev := expireFirstScheduling.Load()
	expireFirstScheduling.Store(enabled)
	t.Cleanup(func() { expireFirstScheduling.Store(prev) })
}

func withTestPool(t *testing.T, pool []*Account, rr int) {
	t.Helper()
	accountMu.Lock()
	prevAccounts, prevRR := accounts, rrIndex
	accounts, rrIndex = pool, rr
	accountMu.Unlock()
	t.Cleanup(func() {
		accountMu.Lock()
		accounts, rrIndex = prevAccounts, prevRR
		accountMu.Unlock()
	})
}

func TestEarliestQuotaEndPrefersEarliestActiveDeadline(t *testing.T) {
	now := time.Date(2026, 11, 21, 10, 0, 0, 0, time.UTC)
	resources := []planResource{
		// 主套餐：扣减周期截止 12-21
		{Status: jsonInt(0), DeductionStartTime: jsonInt(1760000000), DeductionEndTime: jsonStr("2026-12-21 23:59:59")},
		// 赠送包：更早到期 12-01，应胜出
		{Status: jsonInt(0), DeductionStartTime: jsonInt(1760000000), ExpiredTime: jsonStr("2026-12-01 00:00:00")},
		// 长期权益：无截止时间，不参与
		{Status: jsonInt(0), DeductionStartTime: jsonInt(1760000000)},
	}
	end := earliestQuotaEnd(resources, now)
	if end.IsZero() || !end.Equal(time.Date(2026, 12, 1, 0, 0, 0, 0, cnTZ())) {
		t.Fatalf("应取最早的 2026-12-01，得到 %v", end)
	}
}

func TestEarliestQuotaEndIgnoresInvalidAndFutureResources(t *testing.T) {
	now := time.Date(2026, 11, 21, 10, 0, 0, 0, time.UTC)
	resources := []planResource{
		{Status: jsonInt(2), DeductionEndTime: jsonStr("2026-11-22 00:00:00")},                                                // 过期状态，忽略
		{Status: jsonInt(1), DeductionEndTime: jsonStr("2026-11-23 00:00:00")},                                                // 退款状态，忽略
		{Status: jsonInt(0), DeductionStartTime: jsonStr("2026-12-01 00:00:00"), ExpiredTime: jsonStr("2026-11-25 00:00:00")}, // 未到扣减起始，忽略
		{Status: jsonInt(0), DeductionStartTime: jsonInt(1760000000), CycleEndTime: jsonStr("2026-12-10 08:00:00")},           // 有效兜底字段
	}
	end := earliestQuotaEnd(resources, now)
	if end.IsZero() || end.Day() != 10 {
		t.Fatalf("应取 2026-12-10 08:00，得到 %v", end)
	}
}

func TestEarliestQuotaEndNoDeadlineReturnsZero(t *testing.T) {
	now := time.Now()
	if end := earliestQuotaEnd(nil, now); !end.IsZero() {
		t.Fatalf("空资源应返回零值，得到 %v", end)
	}
	if end := earliestQuotaEnd([]planResource{{Status: jsonInt(0)}}, now); !end.IsZero() {
		t.Fatalf("无截止时间资源应返回零值，得到 %v", end)
	}
}

func TestExpireFirstBetter(t *testing.T) {
	early := time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC)
	late := time.Date(2026, 12, 21, 0, 0, 0, 0, time.UTC)
	var zero time.Time
	cases := []struct {
		name           string
		bestEnd        time.Time
		bestDist, dist int
		end            time.Time
		want           bool
	}{
		{"更早到期胜出", late, 0, 1, early, true},
		{"更晚到期不胜出", early, 0, 0, late, false},
		{"到期相同轮询距离近者胜出", early, 1, 0, early, true},
		{"到期相同轮询距离远者不胜出", early, 0, 1, early, false},
		{"已知优先于未知", zero, 0, 1, early, true},
		{"未知不敌已知", early, 0, 0, zero, false},
		{"均未知按轮询距离", zero, 1, 0, zero, true},
	}
	for _, c := range cases {
		if got := expireFirstBetter(c.bestEnd, c.bestDist, c.end, c.dist); got != c.want {
			t.Fatalf("%s: want %t got %t", c.name, c.want, got)
		}
	}
}

func TestNextAccountExpireFirstPicksEarliestDeadline(t *testing.T) {
	withExpireFirst(t, true)
	early := &Account{Path: "workbuddy.json", QuotaExpireAt: time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC)}
	late := &Account{Path: "workbuddy2.json", QuotaExpireAt: time.Date(2026, 12, 21, 0, 0, 0, 0, time.UTC)}
	// 故意把晚到期的账号放在游标位置，证明排序不受轮询游标影响。
	withTestPool(t, []*Account{late, early}, 0)
	for i := 0; i < 3; i++ {
		acc, kind, err := nextAccountForModel("auto", nil)
		if err != nil {
			t.Fatalf("第%d次选号失败: %v", i+1, err)
		}
		if kind != selectionNormal {
			t.Fatalf("期望 normal，得到 %s", kind)
		}
		if acc != early {
			t.Fatalf("应持续选中到期最早的 workbuddy.json，第%d次选中 %s", i+1, acc.Path)
		}
	}
	// 最早到期账号失效后应回退到另一个账号。
	early.Disabled = true
	acc, _, err := nextAccountForModel("auto", nil)
	if err != nil || acc != late {
		t.Fatalf("失效后应选中 workbuddy2.json: acc=%v err=%v", acc, err)
	}
}

func TestNextAccountExpireFirstTieKeepsRoundRobin(t *testing.T) {
	withExpireFirst(t, true)
	same := time.Date(2026, 12, 21, 0, 0, 0, 0, time.UTC)
	a := &Account{Path: "a.json", QuotaExpireAt: same}
	b := &Account{Path: "b.json", QuotaExpireAt: same}
	withTestPool(t, []*Account{a, b}, 0)
	first, _, err := nextAccountForModel("auto", nil)
	if err != nil || first != a {
		t.Fatalf("同到期应先选中游标位置的 a.json: acc=%v err=%v", first, err)
	}
	second, _, err := nextAccountForModel("auto", nil)
	if err != nil || second != b {
		t.Fatalf("同到期应轮换到 b.json: acc=%v err=%v", second, err)
	}
}

func TestNextAccountRoundRobinWhenDisabled(t *testing.T) {
	withExpireFirst(t, false)
	early := &Account{Path: "a.json", QuotaExpireAt: time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC)}
	late := &Account{Path: "b.json", QuotaExpireAt: time.Date(2026, 12, 21, 0, 0, 0, 0, time.UTC)}
	withTestPool(t, []*Account{early, late}, 0)
	first, _, err := nextAccountForModel("auto", nil)
	if err != nil || first != early {
		t.Fatalf("关闭策略应保持轮询先选 a.json: acc=%v err=%v", first, err)
	}
	second, _, err := nextAccountForModel("auto", nil)
	if err != nil || second != late {
		t.Fatalf("关闭策略应轮换到 b.json: acc=%v err=%v", second, err)
	}
}

func TestNextAccountUnknownExpiryRankedLast(t *testing.T) {
	withExpireFirst(t, true)
	known := &Account{Path: "a.json", QuotaExpireAt: time.Date(2026, 12, 21, 0, 0, 0, 0, time.UTC)}
	unknown := &Account{Path: "b.json"}
	withTestPool(t, []*Account{unknown, known}, 0)
	acc, _, err := nextAccountForModel("auto", nil)
	if err != nil || acc != known {
		t.Fatalf("已知到期时间应优先于未知: acc=%v err=%v", acc, err)
	}
	// 均未知时回退轮询。
	known.QuotaExpireAt = time.Time{}
	acc, _, err = nextAccountForModel("auto", nil)
	if err != nil || acc != unknown {
		t.Fatalf("均未知应保持轮询选 b.json: acc=%v err=%v", acc, err)
	}
}

// jsonInt / jsonStr 构造 planResource 的 RawMessage 字段（planTimestamp 接受数字与带引号字符串）。
func jsonInt(v int64) json.RawMessage {
	return json.RawMessage(strconv.FormatInt(v, 10))
}

func jsonStr(s string) json.RawMessage {
	return json.RawMessage(strconv.Quote(s))
}

func cnTZ() *time.Location {
	return time.FixedZone("UTC+8", 8*3600)
}
