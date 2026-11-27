package main

// expire-first 调度策略单元测试（本地 overlay）。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
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
	for i := range resources {
		resources[i].CycleCapacitySizePrecise = jsonInt(100)
		resources[i].CycleCapacityRemainPrecise = jsonInt(100)
	}
	end := earliestQuotaEnd(collectCreditBatches(resources, now), now)
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
	for i := range resources {
		resources[i].CycleCapacitySizePrecise = jsonInt(100)
		resources[i].CycleCapacityRemainPrecise = jsonInt(100)
	}
	end := earliestQuotaEnd(collectCreditBatches(resources, now), now)
	if end.IsZero() || end.Day() != 10 {
		t.Fatalf("应取 2026-12-10 08:00，得到 %v", end)
	}
}

func TestEarliestQuotaEndNoDeadlineReturnsZero(t *testing.T) {
	now := time.Now()
	if end := earliestQuotaEnd(nil, now); !end.IsZero() {
		t.Fatalf("空资源应返回零值，得到 %v", end)
	}
	resources := []planResource{{Status: jsonInt(0), CycleCapacitySizePrecise: jsonInt(100), CycleCapacityRemainPrecise: jsonInt(100)}}
	if end := earliestQuotaEnd(collectCreditBatches(resources, now), now); !end.IsZero() {
		t.Fatalf("无截止时间资源应返回零值，得到 %v", end)
	}
}

func TestEarliestQuotaEndSkipsDepletedBatchesAndUsesCreditCycle(t *testing.T) {
	now := time.Date(2026, 11, 21, 10, 0, 0, 0, time.UTC)
	var resources []planResource
	err := json.Unmarshal([]byte(`[
	  {"Status":3,"CycleCapacitySizePrecise":"200","CycleCapacityRemainPrecise":"0","CycleEndTime":"2026-11-22 00:00:00"},
	  {"Status":0,"CycleEndTime":"2026-11-23 00:00:00"},
	  {"Status":0,"CycleCapacitySizePrecise":100,"CycleCapacityRemainPrecise":12.5,"CycleEndTime":"2026-12-01 00:00:00","DeductionEndTime":"2027-11-21 00:00:00"}
	]`), &resources)
	if err != nil {
		t.Fatal(err)
	}
	want := time.Date(2026, 12, 1, 0, 0, 0, 0, cnTZ())
	batches := collectCreditBatches(resources, now)
	if len(batches) != 2 || batches[0].Remaining != 0 || batches[1].Used != 87.5 || batches[1].Remaining != 12.5 {
		t.Fatalf("应保留已确认的批次用量，跳过缺少余额的批次: %+v", batches)
	}
	if end := earliestQuotaEnd(batches, now); !end.Equal(want) {
		t.Fatalf("仅按有余额批次的积分周期截止排序: got %v, want %v", end, want)
	}
}

func TestEarliestQuotaEndDoesNotUseOuterDatesForSliceCredits(t *testing.T) {
	now := time.Now()
	var resource planResource
	err := json.Unmarshal([]byte(`{
	  "Status":0,"CapacityType":4,"CycleCapacitySizePrecise":"1000","CycleCapacityRemainPrecise":"800",
	  "SlicePeriodUsageDetails":[{"SlicePeriodCapacitySizePrecise":"50","SlicePeriodCapacityRemainPrecise":"7"}]
	}`), &resource)
	if err != nil {
		t.Fatal(err)
	}
	batches := collectCreditBatches([]planResource{resource}, now)
	if len(batches) != 1 || batches[0].Remaining != 7 || batches[0].Used != 43 {
		t.Fatalf("应展示当日切片而不是外层额度: %+v", batches)
	}
	if end := earliestQuotaEnd(batches, now); !end.IsZero() {
		t.Fatalf("未确认切片截止，不能用于到期排序: %v", end)
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
	now := time.Now()
	early := testBatchAccount("workbuddy.json", now, now.Add(time.Hour))
	late := testBatchAccount("workbuddy2.json", now, now.Add(2*time.Hour))
	late.CreditBatches.batches = append([]creditBatch{{Total: 100, Used: 100, ExpiresAt: now.Add(time.Minute).Unix()}}, late.CreditBatches.batches...)
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
	now := time.Now()
	same := now.Add(time.Hour)
	a := testBatchAccount("a.json", now, same)
	b := testBatchAccount("b.json", now, same)
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
	now := time.Now()
	early := testBatchAccount("a.json", now, now.Add(time.Hour))
	late := testBatchAccount("b.json", now, now.Add(2*time.Hour))
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
	now := time.Now()
	known := testBatchAccount("a.json", now, now.Add(time.Hour))
	unknown := testBatchAccount("b.json", now.Add(-time.Hour), now.Add(time.Minute))
	withTestPool(t, []*Account{unknown, known}, 0)
	acc, _, err := nextAccountForModel("auto", nil)
	if err != nil || acc != known {
		t.Fatalf("已知到期时间应优先于未知: acc=%v err=%v", acc, err)
	}
	// 均未知时回退轮询。
	known.CreditBatches.batches[0].ExpiresAt = now.Add(-time.Minute).Unix()
	acc, _, err = nextAccountForModel("auto", nil)
	if err != nil || acc != unknown {
		t.Fatalf("均未知应保持轮询选 b.json: acc=%v err=%v", acc, err)
	}
}

func testBatchAccount(path string, observed, expiry time.Time) *Account {
	return &Account{Path: path, CreditBatches: creditBatchObservation{
		at: observed, batches: []creditBatch{{Total: 100, Remaining: 100, ExpiresAt: expiry.Unix()}},
	}}
}

func TestExpireFirstQuotaRefreshDropsUnconfirmedBatches(t *testing.T) {
	oldClient := cfg.HttpClient
	t.Cleanup(func() { cfg.HttpClient = oldClient })
	mode := "ok"
	expiry := time.Now().Add(time.Hour).Unix()
	cfg.HttpClient = &http.Client{Transport: retryTransportFunc(func(r *http.Request) (*http.Response, error) {
		w := httptest.NewRecorder()
		switch r.URL.Path {
		case "/billing/meter/get-user-resource-summary":
			if mode == "summary failure" {
				w.WriteHeader(http.StatusBadGateway)
			} else {
				_, _ = w.WriteString(`{"code":0,"data":{"Packages":[{"CycleTotalCapacity":"100","CycleUsedCapacity":"1","CycleRemainCapacity":"99"}]}}`)
			}
		case "/billing/meter/get-user-resource":
			if mode == "detail failure" {
				w.WriteHeader(http.StatusBadGateway)
			} else {
				rows := []planResource{{Status: jsonInt(0), CycleEndTime: jsonInt(expiry),
					CycleCapacitySizePrecise: jsonInt(100), CycleCapacityRemainPrecise: jsonInt(99)}}
				_ = json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": map[string]any{"Response": map[string]any{
					"Data": map[string]any{"TotalCount": len(rows), "Accounts": rows},
				}}})
			}
		default:
			t.Fatalf("意外请求: %s", r.URL.Path)
		}
		return w.Result(), nil
	})}
	acc := &Account{Path: "fixture.json", Auth: &StoredAuth{Edition: "intl", Auth: StoredTokens{AccessToken: "fixture"}}}
	ctx := context.Background()
	for _, failure := range []string{"detail failure", "summary failure"} {
		mode = "ok"
		if err := refreshAccountQuota(ctx, acc); err != nil {
			t.Fatal(err)
		}
		before := acc.CreditBatches.current(time.Now())
		if len(before) != 1 || before[0].ExpiresAt != expiry || before[0].Remaining != 99 {
			t.Fatalf("批次未从实际刷新路径进入账号状态: %+v", before)
		}
		mode = failure
		err := refreshAccountQuota(ctx, acc)
		if (err != nil) != (failure == "summary failure") || acc.QuotaRemaining != 99 || len(acc.CreditBatches.current(time.Now())) != 0 {
			t.Fatalf("%s 后应保留总余额但不再使用旧批次: err=%v balance=%v", failure, err, acc.QuotaRemaining)
		}
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
