package main

// expire-first 调度策略（本地 overlay）：
//   开启后，同优先级可用账号按仍有余额的积分批次最早到期时间选择；
//   到期时间相同或未知的账号保持原轮询顺序兜底。未开启时与上游轮询行为完全一致。
//
// 开关只来自环境变量 WB_SCHEDULING_STRATEGY（expireFirst 开启，其余值关闭），
// 不写 config.json：上游 parseRuntimeConfig 对未知字段整份拒绝，写进去会让回滚到
// 上游二进制时配置热加载/启动直接失败。策略选择在模型黑白名单与冷却过滤之后生效，
// 不改变「免费耗尽账号优先 → 有余额账号 → 受控探测」的层级语义。

import (
	"encoding/json"
	"log"
	"os"
	"sort"
	"strings"
	"sync/atomic"
	"time"
)

var expireFirstScheduling atomic.Bool

func expireFirstEnabled() bool { return expireFirstScheduling.Load() }

func init() {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("WB_SCHEDULING_STRATEGY"))) {
	case "expirefirst", "expire_first", "expire-first", "1", "true", "yes", "on":
		expireFirstScheduling.Store(true)
	}
	log.Printf("[调度策略] 到期日最短优先(expireFirst)=%t 来源=WB_SCHEDULING_STRATEGY 未启用时保持轮询", expireFirstScheduling.Load())
}

type creditBatch struct {
	Name      string  `json:"name"`
	Total     float64 `json:"total"`
	Used      float64 `json:"used"`
	Remaining float64 `json:"remaining"`
	ExpiresAt int64   `json:"expiresAt,omitempty"`
}

type creditBatchObservation struct {
	batches []creditBatch
	at      time.Time
}

const sliceCreditCapacityType = 4

type creditSlice struct {
	Total     json.RawMessage `json:"SlicePeriodCapacitySizePrecise"`
	Remaining json.RawMessage `json:"SlicePeriodCapacityRemainPrecise"`
}

func (o creditBatchObservation) current(now time.Time) []creditBatch {
	// 正常每 5 分钟查询一次；停扫后不继续依赖旧的批次余额。
	if o.at.IsZero() || now.Sub(o.at) > 10*time.Minute {
		return nil
	}
	return o.batches
}

func creditAmount(raw json.RawMessage) (float64, bool) {
	value := strings.TrimSpace(strings.Trim(string(raw), `"`))
	if value == "" {
		return 0, false
	}
	amount, err := parseQuotaCapacity(value)
	return amount, err == nil && validRateCredit(amount)
}

func collectCreditBatches(resources []planResource, now time.Time) []creditBatch {
	var batches []creditBatch
	for _, r := range resources {
		active, err := activePlanResource(r, now)
		if err != nil || !active {
			continue
		}
		// 官网的切片额度(4)使用当天 SlicePeriodUsageDetails，不是外层周期余额。
		// 外层周期结束不能代表切片失效日期；未确认切片边界时不据此排序。
		kind, _ := planInteger(r.CapacityType)
		totalRaw, remainingRaw := r.CycleCapacitySizePrecise, r.CycleCapacityRemainPrecise
		if kind == sliceCreditCapacityType {
			var slices []creditSlice
			if err := json.Unmarshal(r.SlicePeriodUsageDetails, &slices); err != nil || len(slices) != 1 {
				continue
			}
			slice := slices[0]
			totalRaw, remainingRaw = slice.Total, slice.Remaining
		}
		total, totalOK := creditAmount(totalRaw)
		remaining, remainingOK := creditAmount(remainingRaw)
		if !totalOK || !remainingOK || total <= 0 || remaining > total {
			continue
		}
		status, _ := planInteger(r.Status)
		if status == 3 {
			remaining = 0
		}
		batch := creditBatch{Name: safePlanName(r.PackageName), Total: total, Used: total - remaining, Remaining: remaining}
		if kind != sliceCreditCapacityType {
			cycle, _, err := planTimestamp(r.CycleEndTime)
			if err != nil {
				continue
			}
			// 积分可能先于订阅失效；失效时间不能晚于权益本身的截止。
			for _, raw := range []json.RawMessage{r.DeductionEndTime, r.ExpiredTime} {
				if end, present, _ := planTimestamp(raw); present && (cycle.IsZero() || end.Before(cycle)) {
					cycle = end
				}
			}
			if !cycle.IsZero() {
				if !cycle.After(now) {
					continue
				}
				batch.ExpiresAt = cycle.Unix()
			}
		}
		batches = append(batches, batch)
	}
	sort.SliceStable(batches, func(i, j int) bool {
		a, b := batches[i].ExpiresAt, batches[j].ExpiresAt
		return a > 0 && (b == 0 || a < b)
	})
	return batches
}

func earliestQuotaEnd(batches []creditBatch, now time.Time) time.Time {
	for _, batch := range batches {
		if batch.Remaining > 0 && batch.ExpiresAt > now.Unix() {
			return time.Unix(batch.ExpiresAt, 0)
		}
	}
	return time.Time{}
}

// expireFirstBetter 判断候选账号是否应取代当前最优：
// 已知到期时间早者优先；两者到期时间相同或均未知时保持轮询距离近者优先；
// 已知到期时间的账号优先于未知到期时间的账号（企业号等无权益数据的账号排最后）。
// dist 为相对轮询游标的距离（i 值），end 零值表示未知。
func expireFirstBetter(bestEnd time.Time, bestDist int, end time.Time, dist int) bool {
	bestKnown, curKnown := !bestEnd.IsZero(), !end.IsZero()
	switch {
	case bestKnown && curKnown:
		if !end.Equal(bestEnd) {
			return end.Before(bestEnd)
		}
		return dist < bestDist
	case curKnown && !bestKnown:
		return true
	case !curKnown && !bestKnown:
		return dist < bestDist
	default:
		return false
	}
}
