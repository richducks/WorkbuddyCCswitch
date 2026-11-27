// pins.go — 账号置顶优先调度。
// 控制台把置顶账号写入 ROOT/pins.json（键=凭据文件名，值=置顶顺序，1 为最优先），
// 网关按文件 mtime/size 热加载；同选择层级候选里置顶账号优先于未置顶账号，
// 置顶账号之间按值升序（先置顶的先消耗）。消耗到保留值以下的账号由 reserve
// 规则挡在调度外，自然轮到顺序里的下一个账号。未置顶账号保持原有策略
// （expireFirst 开启时到期最早优先，否则轮询距离）。
package main

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// 与 reserveJSONPath 一致：网关工作目录即 ROOT。
const pinsJSONPath = "pins.json"

type pinsStore struct {
	mu    sync.Mutex
	mtime int64
	size  int64
	pins  map[string]int
}

var pinConfig = &pinsStore{mtime: -2, pins: map[string]int{}}

// loadLocked 读取 pins.json；仅在 mtime/size 变化时重新解析，解析失败沿用上次配置。
func (p *pinsStore) loadLocked() {
	info, err := os.Stat(pinsJSONPath)
	if err != nil {
		if p.mtime != -1 {
			p.mtime, p.size, p.pins = -1, 0, map[string]int{}
		}
		return
	}
	mtime, size := info.ModTime().UnixNano(), info.Size()
	if mtime == p.mtime && size == p.size {
		return
	}
	data, err := os.ReadFile(pinsJSONPath)
	if err != nil {
		return
	}
	parsed := map[string]int{}
	if err := json.Unmarshal(data, &parsed); err != nil {
		log.Printf("[置顶] 解析 pins.json 失败：%v（沿用上次配置）", err)
		return
	}
	clean := map[string]int{}
	for name, rank := range parsed {
		// 仅接受 >=1 的置顶顺序；0/负数视为未置顶。
		if rank < 1 {
			continue
		}
		clean[filepath.Base(name)] = rank
	}
	p.mtime, p.size, p.pins = mtime, size, clean
}

// pinRankFor 返回账号的置顶顺序；未置顶返回 0。
func pinRankFor(name string) int {
	pinConfig.mu.Lock()
	defer pinConfig.mu.Unlock()
	pinConfig.loadLocked()
	return pinConfig.pins[name]
}

// pinCandidateBetter 判断同层级候选账号是否应取代当前最优：
// 置顶账号优先于未置顶账号；置顶账号之间值小（先置顶）者优先，再按轮询距离；
// 同为未置顶时交给 expireFirstBetter（到期最早优先；expireFirst 关闭时
// 调用方传入零值 end，等价于轮询距离近者优先，与上游行为一致）。
// dist 为相对轮询游标的距离（i 值），end 零值表示到期未知或不参与比较。
func pinCandidateBetter(bestRank, bestDist int, bestEnd time.Time, rank, dist int, end time.Time) bool {
	if rank != bestRank {
		if bestRank == 0 {
			return rank > 0
		}
		return rank > 0 && rank < bestRank
	}
	if rank > 0 {
		return dist < bestDist
	}
	return expireFirstBetter(bestEnd, bestDist, end, dist)
}
