package envfile

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"time"
)

// The lock beside the target file (bootstrap_secrets.py:123-125, :276-335).
const (
	LockSuffix  = ".lazyaf-bootstrap.lock"
	LockTimeout = 20 * time.Second
	LockStale   = 120 * time.Second
	lockPoll    = 50 * time.Millisecond
)

// LockTimeoutError is "someone else has held the lock for the whole
// timeout". It names the file so the operator can decide whether to delete
// it.
type LockTimeoutError struct {
	Path string
}

func (e *LockTimeoutError) Error() string {
	return fmt.Sprintf("another lazyaf init run is holding %s (or it crashed and left it behind). "+
		"Delete that file if you are sure nothing is running.", e.Path)
}

// FileLock is a cooperative lock beside the target file.
//
// O_CREATE|O_EXCL is atomic on every platform this runs on (NTFS included),
// which is the whole mechanism. A lock older than LockStale is broken rather
// than waited on, because the alternative is a killed process wedging the
// next person's setup forever.
type FileLock struct {
	Path string
	f    *os.File
}

// LockPath is where the lock for target lives.
func LockPath(target string) string { return target + LockSuffix }

// Lock acquires the lock for target, polling until timeout.
func Lock(target string, timeout time.Duration) (*FileLock, error) {
	lock := &FileLock{Path: LockPath(target)}
	deadline := time.Now().Add(timeout)
	for {
		f, err := os.OpenFile(lock.Path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err == nil {
			_, _ = f.WriteString(strconv.Itoa(os.Getpid()))
			lock.f = f
			return lock, nil
		}
		if !errors.Is(err, os.ErrExist) {
			return nil, err
		}
		if lock.breakIfStale() {
			continue
		}
		if time.Now().After(deadline) {
			return nil, &LockTimeoutError{Path: lock.Path}
		}
		time.Sleep(lockPoll)
	}
}

// breakIfStale unlinks a lock older than LockStale and says whether the
// caller should retry the create immediately.
func (l *FileLock) breakIfStale() bool {
	info, err := os.Stat(l.Path)
	if err != nil {
		return true // vanished under us: retry the create
	}
	if time.Since(info.ModTime()) <= LockStale {
		return false
	}
	return os.Remove(l.Path) == nil
}

// Unlock releases and removes the lock. Closed before it is unlinked: an
// open file cannot be deleted on Windows.
func (l *FileLock) Unlock() {
	if l.f != nil {
		_ = l.f.Close()
		l.f = nil
	}
	_ = os.Remove(l.Path)
}
