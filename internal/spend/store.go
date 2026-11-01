// ABOUTME: The Meter's file-backed store: a bbolt database on the pod's volume holding the cluster
// ABOUTME: tally, each session's spend and the runtime cost cap, so a restart does not reset spend.
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

// state is everything the store holds. costCap is set only when a runtime cap was stored.
type state struct {
	totals     Totals
	session    map[sessionKey]float64
	costCap    float64
	costCapSet bool
}

func (s *Store) load() (state, error) {
	st := state{session: map[sessionKey]float64{}}
	err := s.db.View(func(tx *bolt.Tx) error {
		meter := tx.Bucket(meterBucket)
		if raw := meter.Get(totalsKey); raw != nil {
			if err := json.Unmarshal(raw, &st.totals); err != nil {
				return fmt.Errorf("totals: %w", err)
			}
		}
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

// saveSpend writes the cluster tally and, when session is set, that session's spend, in one commit.
func (s *Store) saveSpend(t Totals, session *sessionKey, sessionUSD float64) error {
	raw, err := json.Marshal(t)
	if err != nil {
		return err
	}
	err = s.db.Update(func(tx *bolt.Tx) error {
		if err := tx.Bucket(meterBucket).Put(totalsKey, raw); err != nil {
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

// formatUSD writes the shortest decimal that parses back to exactly usd.
func formatUSD(usd float64) []byte { return strconv.AppendFloat(nil, usd, 'g', -1, 64) }
