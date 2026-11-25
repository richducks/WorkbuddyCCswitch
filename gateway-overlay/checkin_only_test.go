package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestCheckinOnlyAccountExcludedFromModelPolicy(t *testing.T) {
	path := filepath.Join(t.TempDir(), "workbuddy2.json")
	acc := &Account{Path: path, Auth: &StoredAuth{}}
	acc.Auth.Auth.AccessToken = "fixture"
	acc.Auth.Account.UID = "fixture-user"
	duplicate := &Account{Path: filepath.Join(filepath.Dir(path), "workbuddy3.json"), Auth: acc.Auth}
	previous, previousIndex := accounts, rrIndex
	accounts, rrIndex = []*Account{acc, duplicate}, 0
	t.Cleanup(func() { accounts, rrIndex = previous, previousIndex })
	marker := path + ".checkin-only"
	if err := os.WriteFile(marker, nil, 0600); err != nil {
		t.Fatal(err)
	}
	for _, model := range []string{"all", "test-model"} {
		if allowed, _ := modelAccountAllowed(model, acc, []*Account{acc}); allowed {
			t.Fatalf("check-in-only account eligible for %s", model)
		}
	}
	if picked, _, err := nextAccountForModel("test-model", nil); picked != nil || err == nil {
		t.Fatal("neither copy of the check-in-only identity should be selected")
	}
	if result := probeAccountModel(context.Background(), acc, "test-model"); result.Status != "skipped" {
		t.Fatalf("check-in-only account model probe was not skipped: %s", result.Status)
	}
	if !checkinTargetLocked(acc).eligible {
		t.Fatal("account should still be eligible for automatic check-in")
	}
	if err := os.Remove(marker); err != nil {
		t.Fatal(err)
	}
	if picked, _, err := nextAccountForModel("test-model", nil); picked != acc || err != nil {
		t.Fatalf("promoted account was not selected: %v", err)
	}
}
