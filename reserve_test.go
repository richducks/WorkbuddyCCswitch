package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReserveStoreHotReloadsOnMtimeChange(t *testing.T) {
	chdirTemp(t)
	if err := os.WriteFile("reserve.json", []byte(`{"a.json": 120, "nested/b.json": 30, "zero.json": 0}`), 0o600); err != nil {
		t.Fatal(err)
	}
	reserveConfig = &reserveStore{mtime: -2, reserves: map[string]float64{}}
	if got := reserveFor("a.json"); got != 120 {
		t.Fatalf("a.json reserve = %v, want 120", got)
	}
	// 嵌套路径写入的键按文件名归一化。
	if got := reserveFor("b.json"); got != 30 {
		t.Fatalf("b.json reserve = %v, want 30", got)
	}
	// 显式写入的 0（不保留）保留在配置里。
	if got := reserveFor("zero.json"); got != 0 {
		t.Fatalf("zero.json reserve = %v, want 0", got)
	}
	// 未设置的账号返回默认保留额度。
	if got := reserveFor("c.json"); got != defaultReserve {
		t.Fatalf("c.json reserve = %v, want %v", got, defaultReserve)
	}
	// 文件消失后配置清空，回到默认值。
	if err := os.Remove("reserve.json"); err != nil {
		t.Fatal(err)
	}
	if got := reserveFor("a.json"); got != defaultReserve {
		t.Fatalf("after remove a.json reserve = %v, want %v", got, defaultReserve)
	}
}

func TestReserveBlockedSkipsAccountBelowReserve(t *testing.T) {
	chdirTemp(t)
	if err := os.WriteFile("reserve.json", []byte(`{"a.json": 120, "zero.json": 0}`), 0o600); err != nil {
		t.Fatal(err)
	}
	reserveConfig = &reserveStore{mtime: -2, reserves: map[string]float64{}}

	if reserveBlocked(nil) {
		t.Fatal("nil account must not block")
	}
	// 存储值 120：剩余 50 低于保留额度，阻断；等于 120 不阻断。
	below := &Account{Path: filepath.Join("accounts", "a.json"), QuotaKnown: true, QuotaRemaining: 50}
	if !reserveBlocked(below) {
		t.Fatal("remaining 50 < reserve 120 must block")
	}
	equal := &Account{Path: filepath.Join("accounts", "a.json"), QuotaKnown: true, QuotaRemaining: 120}
	if reserveBlocked(equal) {
		t.Fatal("remaining == reserve must not block")
	}
	// 显式 0（不保留）：不阻断。
	kept := &Account{Path: "zero.json", QuotaKnown: true, QuotaRemaining: 1}
	if reserveBlocked(kept) {
		t.Fatal("reserve 0 must not block")
	}
	// 未自定义：默认 100 生效，剩余 50 阻断、150 不阻断。
	unset50 := &Account{Path: "unset.json", QuotaKnown: true, QuotaRemaining: 50}
	if !reserveBlocked(unset50) {
		t.Fatal("remaining 50 < default reserve 100 must block")
	}
	unset150 := &Account{Path: "unset.json", QuotaKnown: true, QuotaRemaining: 150}
	if reserveBlocked(unset150) {
		t.Fatal("remaining 150 >= default reserve 100 must not block")
	}
	// 额度未知或无限额度时不生效。
	unknown := &Account{Path: "a.json", QuotaKnown: false, QuotaRemaining: 50}
	if reserveBlocked(unknown) {
		t.Fatal("quota unknown must not block")
	}
	unlimited := &Account{Path: "a.json", QuotaKnown: true, QuotaUnlimited: true, QuotaRemaining: 50}
	if reserveBlocked(unlimited) {
		t.Fatal("quota unlimited must not block")
	}
}
