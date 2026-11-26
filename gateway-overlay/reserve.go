// reserve.go — 账号保留额度。
// 控制台把每个账号的保留额度（积分数）写入 ROOT/reserve.json（键=凭据文件名），
// 网关按文件 mtime/size 热加载；调度时跳过剩余积分低于保留额度的账号，
// 让请求落到未低于保留额度的账号上。额度未知（quotaKnown=false）或无限额度时不生效。
package main

import (
	"encoding/json"
	"log"
	"math"
	"os"
	"path/filepath"
	"sync"
)

// 与 main.go 的 statusSnapshotFile 一致：网关工作目录即 ROOT。
const reserveJSONPath = "reserve.json"

// defaultReserve：账号未自定义保留额度时的默认值（积分）。
const defaultReserve = 100.0

type reserveStore struct {
	mu       sync.Mutex
	mtime    int64
	size     int64
	reserves map[string]float64
}

var reserveConfig = &reserveStore{mtime: -2, reserves: map[string]float64{}}

// loadLocked 读取 reserve.json；仅在 mtime/size 变化时重新解析，解析失败沿用上次配置。
func (r *reserveStore) loadLocked() {
	info, err := os.Stat(reserveJSONPath)
	if err != nil {
		if r.mtime != -1 {
			r.mtime, r.size, r.reserves = -1, 0, map[string]float64{}
		}
		return
	}
	mtime, size := info.ModTime().UnixNano(), info.Size()
	if mtime == r.mtime && size == r.size {
		return
	}
	data, err := os.ReadFile(reserveJSONPath)
	if err != nil {
		return
	}
	parsed := map[string]float64{}
	if err := json.Unmarshal(data, &parsed); err != nil {
		log.Printf("[Reserve] 解析 reserve.json 失败：%v（沿用上次配置）", err)
		return
	}
	clean := map[string]float64{}
	for name, value := range parsed {
		if math.IsInf(value, 0) || math.IsNaN(value) || value < 0 {
			continue
		}
		// 保留显式写入的 0（不保留），负数/非法值忽略。
		clean[filepath.Base(name)] = value
	}
	r.mtime, r.size, r.reserves = mtime, size, clean
}

// reserveFor 返回账号的保留额度（积分数；未自定义时返回默认值，显式 0 表示不保留）。
func reserveFor(name string) float64 {
	reserveConfig.mu.Lock()
	defer reserveConfig.mu.Unlock()
	reserveConfig.loadLocked()
	if value, ok := reserveConfig.reserves[name]; ok {
		return value
	}
	return defaultReserve
}

// reserveBlocked 判断账号剩余积分是否低于保留额度。
func reserveBlocked(acc *Account) bool {
	if acc == nil || acc.Path == "" {
		return false
	}
	reserve := reserveFor(filepath.Base(acc.Path))
	if reserve <= 0 || acc.QuotaUnlimited || !acc.QuotaKnown {
		return false
	}
	return acc.QuotaRemaining < reserve
}
