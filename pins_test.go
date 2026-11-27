package main

import (
	"os"
	"testing"
	"time"
)

func TestPinRankForHotReload(t *testing.T) {
	defer os.Remove("pins.json")
	if got := pinRankFor("a.json"); got != 0 {
		t.Fatalf("未置顶账号应返回 0，得到 %d", got)
	}
	if err := os.WriteFile("pins.json", []byte(`{"a.json": 2, "b.json": 1, "c.json": 0, "d.json": -3}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := pinRankFor("a.json"); got != 2 {
		t.Fatalf("a.json 置顶顺序应为 2，得到 %d", got)
	}
	if got := pinRankFor("b.json"); got != 1 {
		t.Fatalf("b.json 置顶顺序应为 1，得到 %d", got)
	}
	if got := pinRankFor("c.json"); got != 0 {
		t.Fatalf("0 应视为未置顶，得到 %d", got)
	}
	if got := pinRankFor("d.json"); got != 0 {
		t.Fatalf("负数应视为未置顶，得到 %d", got)
	}
	if err := os.WriteFile("pins.json", []byte(`not json`), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := pinRankFor("a.json"); got != 2 {
		t.Fatalf("解析失败应沿用上次配置，得到 %d", got)
	}
}

func TestPinCandidateBetter(t *testing.T) {
	zero := time.Time{}
	end := zero.Add(time.Hour)
	// 置顶优先于未置顶。
	if !pinCandidateBetter(0, 0, zero, 1, 5, zero) {
		t.Fatal("置顶账号应优先于未置顶账号")
	}
	if pinCandidateBetter(1, 5, zero, 0, 0, zero) {
		t.Fatal("未置顶账号不应取代置顶账号")
	}
	// 置顶账号之间值小者优先，其次轮询距离。
	if !pinCandidateBetter(2, 0, zero, 1, 5, zero) {
		t.Fatal("先置顶（值小）者应优先")
	}
	if pinCandidateBetter(1, 5, zero, 2, 0, zero) {
		t.Fatal("后置顶（值大）者不应取代先置顶者")
	}
	if !pinCandidateBetter(1, 5, zero, 1, 2, zero) {
		t.Fatal("同置顶顺序应按轮询距离取近者")
	}
	// 未置顶且 expireFirst 关闭（end 全零）：轮询距离近者优先。
	if pinCandidateBetter(0, 0, zero, 0, 3, zero) {
		t.Fatal("expireFirst 关闭时远距离候选不应取代近距离候选")
	}
	if !pinCandidateBetter(0, 3, zero, 0, 1, zero) {
		t.Fatal("expireFirst 关闭时应取轮询距离近者")
	}
	// 未置顶且 expireFirst 开启：到期最早优先，未知到期排最后。
	if !pinCandidateBetter(0, 0, zero, 0, 5, end) {
		t.Fatal("已知到期时间的候选应优先于未知到期时间的候选")
	}
	if !pinCandidateBetter(0, 5, end, 0, 1, zero.Add(2*time.Hour)) {
		t.Fatal("到期更早的候选应胜出")
	}
}
