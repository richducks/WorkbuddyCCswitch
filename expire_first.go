package main

// expire-first 调度策略（本地 overlay）：
//   开启后，同优先级可用账号按「额度周期到期时间最早优先」选择，把快到期的积分先用掉；
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

// earliestQuotaEnd 返回权益资源里最早的额度周期截止时间（积分到期时间）。
// 单个资源的截止字段优先级与 activePlanResource 一致：DeductionEndTime > ExpiredTime > CycleEndTime；
// 只统计状态有效（0 正常 / 3 积分用尽）、已到扣减起始时间且截止仍在未来的资源。
// 赠送包等一切带截止时间的权益都参与：它们同样代表「到期即作废的积分」。
// 没有截止时间的长期权益不参与；没有任何可用截止时间时返回零值（视为未知）。
func earliestQuotaEnd(resources []planResource, now time.Time) time.Time {
	var earliest time.Time
	for _, r := range resources {
		status, err := planInteger(r.Status)
		if err != nil || (status != 0 && status != 3) {
			continue
		}
		if start, present, err := planTimestamp(r.DeductionStartTime); err == nil && present && now.Before(start) {
			continue
		}
		for _, raw := range []json.RawMessage{r.DeductionEndTime, r.ExpiredTime, r.CycleEndTime} {
			end, present, err := planTimestamp(raw)
			if err != nil {
				break // 该资源时间字段异常，放弃这个资源，不影响其他资源
			}
			if !present {
				continue
			}
			if end.After(now) && (earliest.IsZero() || end.Before(earliest)) {
				earliest = end
			}
			break
		}
	}
	return earliest
}

// setAccountQuotaExpiry 把额度到期时间写入账号状态；查询失败时不清理旧值，避免抖动。
func setAccountQuotaExpiry(acc *Account, resources []planResource, now time.Time) {
	end := earliestQuotaEnd(resources, now)
	accountMu.Lock()
	acc.QuotaExpireAt = end
	accountMu.Unlock()
	if !end.IsZero() {
		log.Printf("[额度到期] 账号 %s 最早积分周期截止=%s 来源=完整权益查询", acc.Path, end.Format("2006-01-02 15:04:05"))
	}
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
