// ABOUTME: Tests for the Meter's store: the tally, each session's spend and the runtime cost cap survive
// ABOUTME: reopening the file, and a store that cannot be written or is held elsewhere fails to open.
// SPDX-License-Identifier: Apache-2.0
package spend

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	bolt "go.etcd.io/bbolt"
)

func openStore(t *testing.T, path string) *Store {
	t.Helper()
	st, err := OpenStore(path)
	if err != nil {
		t.Fatal(err)
	}
	return st
}

// reopen closes st, as a process exiting does, and opens a meter on the same file.
func reopen(t *testing.T, st *Store, path string, envCap float64) (*Meter, *Store) {
	t.Helper()
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	st = openStore(t, path)
	t.Cleanup(func() { _ = st.Close() })
	m, err := NewMeter(envCap, st)
	if err != nil {
		t.Fatal(err)
	}
	return m, st
}

func TestSpendSurvivesReopeningTheStore(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spend.db")
	p := Prices{In: 0.001, Out: 0.005}
	st := openStore(t, path)
	m, err := NewMeter(0, st)
	if err != nil {
		t.Fatal(err)
	}
	record(t, m, p, Usage{In: 1234, Out: 567, Total: 1801}, "a")
	record(t, m, p, Usage{In: 10, Out: 3, Total: 13}, "a")
	record(t, m, p, Usage{In: 7, Out: 1, Total: 8}, "")
	before, beforeA := m.Totals(), m.SessionUSD("a")

	m, _ = reopen(t, st, path, 0)
	if got := m.Totals(); got != before {
		t.Errorf("Totals after reopening = %+v, want %+v", got, before)
	}
	// Exactly, not approximately: the stored decimal parses back to the same float.
	if got := m.SessionUSD("a"); got != beforeA {
		t.Errorf("SessionUSD(a) after reopening = %v, want %v", got, beforeA)
	}
	if got := m.SessionUSD("b"); got != 0 {
		t.Errorf("a session never seen has spent %v", got)
	}
	record(t, m, p, Usage{In: 1000}, "a")
	if got := m.Totals().Requests; got != before.Requests+1 {
		t.Errorf("Requests = %d, want the tally to carry on from %d", got, before.Requests)
	}
}

func TestABudgetTrippedBeforeReopeningStillHolds(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spend.db")
	p := Prices{In: 1}
	st := openStore(t, path)
	m, err := NewMeter(0, st)
	if err != nil {
		t.Fatal(err)
	}
	record(t, m, p, Usage{In: 1000}, "a")
	m, _ = reopen(t, st, path, 0)
	if got := m.Capped("a", true, 1); got != Budget {
		t.Errorf("Capped(a) after reopening = %v, want Budget", got)
	}
	if got := m.Capped("fresh", true, 1); got != None {
		t.Errorf("a fresh session after reopening = %v, want None", got)
	}
}

func TestALongSessionIdIsStored(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spend.db")
	st := openStore(t, path)
	m, err := NewMeter(0, st)
	if err != nil {
		t.Fatal(err)
	}
	// Longer than a bbolt key may be, so it only fits because it is hashed.
	long := strings.Repeat("s", 40000)
	record(t, m, Prices{In: 1}, Usage{In: 1000}, long)
	m, _ = reopen(t, st, path, 0)
	if got := m.SessionUSD(long); got != 1 {
		t.Errorf("SessionUSD = %v, want 1", got)
	}
}

func TestRuntimeCostCapSurvivesReopening(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spend.db")
	st := openStore(t, path)
	m, err := NewMeter(25, st)
	if err != nil {
		t.Fatal(err)
	}
	if err := m.SetCostCap(2); err != nil {
		t.Fatal(err)
	}
	m, st = reopen(t, st, path, 25)
	if got := m.CostCap(); got != 2 {
		t.Errorf("CostCap after reopening = %v, want the runtime 2", got)
	}
	// COST_CAP_USD lowered below the stored cap on the restart: the env value is the lower backstop.
	m, st = reopen(t, st, path, 1)
	if got := m.CostCap(); got != 1 {
		t.Errorf("CostCap = %v, want COST_CAP_USD 1", got)
	}
	// Raised again, the stored lowering still applies, since it was never restored to the default.
	m, st = reopen(t, st, path, 30)
	if got := m.CostCap(); got != 2 {
		t.Errorf("CostCap = %v, want the runtime 2", got)
	}
	// Restoring the default stores no runtime cap, so the next COST_CAP_USD is what applies.
	if err := m.SetCostCap(30); err != nil {
		t.Fatal(err)
	}
	m, _ = reopen(t, st, path, 40)
	if got := m.CostCap(); got != 40 {
		t.Errorf("CostCap = %v, want COST_CAP_USD 40", got)
	}
}

func TestStoreOpenFailsRatherThanMeteringIntoMemory(t *testing.T) {
	dir := t.TempDir()
	readOnly := filepath.Join(dir, "ro")
	if err := os.Mkdir(readOnly, 0o500); err != nil {
		t.Fatal(err)
	}
	for name, path := range map[string]string{
		"unwritable directory": filepath.Join(readOnly, "spend.db"),
		"missing directory":    filepath.Join(dir, "absent", "spend.db"),
		"a directory":          dir,
	} {
		t.Run(name, func(t *testing.T) {
			st, err := OpenStore(path)
			if err == nil {
				_ = st.Close()
				t.Fatal("OpenStore succeeded")
			}
			if !strings.HasPrefix(err.Error(), "spend store "+path+": ") {
				t.Errorf("error = %q", err)
			}
		})
	}
}

func TestStoreHeldByAnotherOpenFails(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spend.db")
	st := openStore(t, path)
	defer st.Close()
	if second, err := OpenStore(path); err == nil {
		_ = second.Close()
		t.Fatal("a second open of a held store succeeded")
	} else if err.Error() != "spend store "+path+": held by another process" {
		t.Errorf("error = %q", err)
	}
}

func TestRecordReportsAFailedCommitAndKeepsTheSpendInMemory(t *testing.T) {
	st := openStore(t, filepath.Join(t.TempDir(), "spend.db"))
	m, err := NewMeter(0, st)
	if err != nil {
		t.Fatal(err)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Record(Usage{In: 1000}, Prices{In: 1}, "a"); err == nil || !strings.HasPrefix(err.Error(), "spend store: ") {
		t.Errorf("Record on a closed store = %v", err)
	}
	if got := m.Capped("a", true, 1); got != Budget {
		t.Errorf("Capped = %v: the uncommitted spend must still hold the budget in this process", got)
	}
	if err := m.SetCostCap(0.5); err == nil {
		t.Error("SetCostCap on a closed store succeeded")
	}
	if got := m.CostCap(); got != 0 {
		t.Errorf("CostCap = %v: a cap that was not stored must not apply", got)
	}
}

func TestACorruptStoreFailsToLoad(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spend.db")
	st := openStore(t, path)
	defer st.Close()
	if err := st.db.Update(func(tx *bolt.Tx) error { return tx.Bucket(meterBucket).Put(totalsKey, []byte("{")) }); err != nil {
		t.Fatal(err)
	}
	if _, err := NewMeter(0, st); err == nil || !strings.HasPrefix(err.Error(), "spend store: reading "+path+": totals: ") {
		t.Errorf("NewMeter = %v", err)
	}
}

func TestResetClearsSpendSessionsAndTheRuntimeCostCap(t *testing.T) {
	path := filepath.Join(t.TempDir(), "spend.db")
	p := Prices{In: 1}
	st := openStore(t, path)
	m, err := NewMeter(25, st)
	if err != nil {
		t.Fatal(err)
	}
	record(t, m, p, Usage{In: 1000, Total: 1000}, "a")
	record(t, m, p, Usage{In: 500, Total: 500}, "b")
	record(t, m, p, Usage{In: 250, Total: 250}, "")
	if err := m.SetCostCap(2); err != nil {
		t.Fatal(err)
	}

	got, err := st.Reset()
	if err != nil {
		t.Fatal(err)
	}
	want := Cleared{Totals: Totals{Requests: 3, InputTokens: 1750, TotalTokens: 1750, USD: 1.75}, Sessions: 2, CostCap: 2, CostCapSet: true}
	if got != want {
		t.Errorf("Reset = %+v, want %+v", got, want)
	}

	m, st = reopen(t, st, path, 25)
	if got := m.Totals(); got != (Totals{}) {
		t.Errorf("Totals after Reset = %+v, want zero", got)
	}
	if got := m.SessionUSD("a"); got != 0 {
		t.Errorf("SessionUSD(a) after Reset = %v, want 0", got)
	}
	if got := m.CostCap(); got != 25 {
		t.Errorf("CostCap after Reset = %v, want COST_CAP_USD 25", got)
	}
	// The store still meters after a reset, from $0.
	record(t, m, p, Usage{In: 1000, Total: 1000}, "a")
	if got := m.SessionUSD("a"); got != 1 {
		t.Errorf("SessionUSD(a) = %v, want 1", got)
	}

	// A store with nothing on it resets to the same nothing.
	empty := openStore(t, filepath.Join(t.TempDir(), "empty.db"))
	defer empty.Close()
	if got, err := empty.Reset(); err != nil || got != (Cleared{}) {
		t.Errorf("Reset of an empty store = %+v, %v", got, err)
	}
}

func TestResetOfAClosedStoreFails(t *testing.T) {
	st := openStore(t, filepath.Join(t.TempDir(), "spend.db"))
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Reset(); err == nil || !strings.HasPrefix(err.Error(), "spend store: ") {
		t.Errorf("Reset on a closed store = %v", err)
	}
}
