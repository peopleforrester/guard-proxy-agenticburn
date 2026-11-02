// ABOUTME: The Meter's file-backed store: a bbolt database on the pod's volume holding the cluster tally,
// ABOUTME: its split by model, each session's spend and the runtime cost cap, so a restart resets none.
// SPDX-License-Identifier: Apache-2.0
package spend

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"

	bolt "go.etcd.io/bbolt"
)

// lockWait bounds how long OpenStore waits for another process to let go of the file. Two processes
// metering into one store would each overwrite the other's tally, so a held lock fails startup.
const lockWait = 2 * time.Second

var (
	meterBucket   = []byte("meter")
	sessionBucket = []byte("sessions")
	totalsKey     = []byte("totals")
	modelsKey     = []byte("models")
	costCapKey    = []byte("cost_cap")
)

// sessionKey is how a session is keyed in memory and on disk. The id is whatever the browser sends, so
// it is hashed to a fixed size that always fits a bbolt key.
type sessionKey [sha256.Size]byte

func keyOf(session string) sessionKey { return sha256.Sum256([]byte(session)) }

// Store is the Meter's durable state. Every write is one bbolt transaction, which fsyncs on commit.
type Store struct {
	db *bolt.DB
}

// OpenStore opens the store at path, creating the file when it is absent. A path that cannot be opened
// for writing, or a file another process holds, is an error: spend is never metered into memory alone.
func OpenStore(path string) (*Store, error) {
	db, err := bolt.Open(path, 0o600, &bolt.Options{Timeout: lockWait})
	if errors.Is(err, bolt.ErrTimeout) {
		return nil, fmt.Errorf("spend store %s: held by another process", path)
	}
	if err != nil {
		return nil, fmt.Errorf("spend store %s: %w", path, err)
	}
	err = db.Update(func(tx *bolt.Tx) error {
		for _, b := range [][]byte{meterBucket, sessionBucket} {
			if _, err := tx.CreateBucketIfNotExists(b); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("spend store %s: %w", path, err)
	}
	return &Store{db: db}, nil
}

// Close releases the file.
func (s *Store) Close() error { return s.db.Close() }

// state is everything the store holds. costCap is set only when a runtime cap was stored. unsplit is
// spend with no split by model, which a store written before guard-proxy-agenticburn#38 holds: every
// commit since writes the split with the tally, so no model can be named for it.
type state struct {
	totals     Totals
	models     map[string]float64
	unsplit    bool
	session    map[sessionKey]float64
	costCap    float64
	costCapSet bool
}

func (s *Store) load() (state, error) {
	st := state{models: map[string]float64{}, session: map[sessionKey]float64{}}
	err := s.db.View(func(tx *bolt.Tx) error {
		meter := tx.Bucket(meterBucket)
		if raw := meter.Get(totalsKey); raw != nil {
			if err := json.Unmarshal(raw, &st.totals); err != nil {
				return fmt.Errorf("totals: %w", err)
			}
		}
		raw := meter.Get(modelsKey)
		if raw != nil {
			if err := json.Unmarshal(raw, &st.models); err != nil {
				return fmt.Errorf("spend by model: %w", err)
			}
		}
		st.unsplit = raw == nil && st.totals.USD != 0
		if raw := meter.Get(costCapKey); raw != nil {
			c, err := strconv.ParseFloat(string(raw), 64)
			if err != nil {
				return fmt.Errorf("cost cap: %w", err)
			}
			st.costCap, st.costCapSet = c, true
		}
		return tx.Bucket(sessionBucket).ForEach(func(k, v []byte) error {
			if len(k) != sha256.Size {
				return fmt.Errorf("session key of %d bytes", len(k))
			}
			usd, err := strconv.ParseFloat(string(v), 64)
			if err != nil {
				return fmt.Errorf("session spend: %w", err)
			}
			st.session[sessionKey(k)] = usd
			return nil
		})
	})
	if err != nil {
		return state{}, fmt.Errorf("spend store: reading %s: %w", s.db.Path(), err)
	}
	return st, nil
}

// saveSpend writes the cluster tally, its split by model and, when session is set, that session's spend,
// in one commit.
func (s *Store) saveSpend(t Totals, models map[string]float64, session *sessionKey, sessionUSD float64) error {
	raw, err := json.Marshal(t)
	if err != nil {
		return err
	}
	byModel, err := json.Marshal(models)
	if err != nil {
		return err
	}
	err = s.db.Update(func(tx *bolt.Tx) error {
		meter := tx.Bucket(meterBucket)
		if err := meter.Put(totalsKey, raw); err != nil {
			return err
		}
		if err := meter.Put(modelsKey, byModel); err != nil {
			return err
		}
		if session == nil {
			return nil
		}
		return tx.Bucket(sessionBucket).Put(session[:], formatUSD(sessionUSD))
	})
	if err != nil {
		return fmt.Errorf("spend store: %w", err)
	}
	return nil
}

// saveCostCap stores a runtime cost cap, or deletes it when set is false, so COST_CAP_USD applies.
func (s *Store) saveCostCap(usd float64, set bool) error {
	err := s.db.Update(func(tx *bolt.Tx) error {
		b := tx.Bucket(meterBucket)
		if !set {
			return b.Delete(costCapKey)
		}
		return b.Put(costCapKey, formatUSD(usd))
	})
	if err != nil {
		return fmt.Errorf("spend store: %w", err)
	}
	return nil
}

// Cleared is what Reset took off a store. CostCap is set only when a runtime cap was stored.
type Cleared struct {
	Totals     Totals
	Sessions   int
	CostCap    float64
	CostCapSet bool
}

// Reset clears the cluster tally and its split by model, every session's spend and the runtime cost cap
// in one commit, so the next process starts from $0 under COST_CAP_USD. It is the instructor's reset
// between deliveries, run against a stopped proxy: no HTTP path reaches it, since /toggle has no auth and
// the agent's tool pod can reach the proxy.
func (s *Store) Reset() (Cleared, error) {
	st, err := s.load()
	if err != nil {
		return Cleared{}, err
	}
	err = s.db.Update(func(tx *bolt.Tx) error {
		meter := tx.Bucket(meterBucket)
		for _, k := range [][]byte{totalsKey, modelsKey, costCapKey} {
			if err := meter.Delete(k); err != nil {
				return err
			}
		}
		if err := tx.DeleteBucket(sessionBucket); err != nil {
			return err
		}
		_, err := tx.CreateBucket(sessionBucket)
		return err
	})
	if err != nil {
		return Cleared{}, fmt.Errorf("spend store: %w", err)
	}
	return Cleared{Totals: st.totals, Sessions: len(st.session), CostCap: st.costCap, CostCapSet: st.costCapSet}, nil
}

// formatUSD writes the shortest decimal that parses back to exactly usd.
func formatUSD(usd float64) []byte { return strconv.AppendFloat(nil, usd, 'g', -1, 64) }
