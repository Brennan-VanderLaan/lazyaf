package envfile

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"time"
)

// Line is one physical line of a .env about to be written: either verbatim
// Text, or the assignment `Key=<Secret>` when Secret is set. Keeping the
// secret out of Text is what lets Plan return a document nobody can print.
type Line struct {
	Text   string
	Key    string
	Secret Secret
}

// Verbatim is a Line that is written as-is.
func Verbatim(text string) Line { return Line{Text: text} }

// Assignment is the Line `key=<secret>`.
func Assignment(key string, secret Secret) Line { return Line{Key: key, Secret: secret} }

// write emits one line without its ending.
func (l Line) write(w *bufio.Writer) error {
	if l.Secret.IsZero() {
		_, err := w.WriteString(l.Text)
		return err
	}
	if _, err := w.WriteString(l.Key + "="); err != nil {
		return err
	}
	_, err := l.Secret.WriteTo(w)
	return err
}

// renameAttempts bounds the Windows retry in AtomicWrite: a concurrent
// reader that has the target open without FILE_SHARE_DELETE makes the
// replace fail for the milliseconds its read takes. 40 x 25 ms is two
// seconds, far past any read of a .env.
const (
	renameAttempts = 40
	renameBackoff  = 25 * time.Millisecond
)

// AtomicWrite writes lines to path via a sibling temp file and a rename
// (bootstrap_secrets.py:338-368).
//
// A reader either sees the old file or the new one, never a truncated one -
// and the temp file is created O_EXCL 0600 so the secret is never briefly
// readable by everyone on a shared box. The rename is os.Rename, which on
// Windows is MoveFileEx(MOVEFILE_REPLACE_EXISTING): the same replace
// semantics as os.replace. Then chmod 0600, best effort (a no-op that means
// nothing on Windows, as _restrict_permissions was).
func AtomicWrite(path string, lines []Line, newline string) (err error) {
	tmp := path + ".tmp-" + strconv.Itoa(os.Getpid())
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	committed := false
	defer func() {
		if !committed {
			_ = f.Close()
			_ = os.Remove(tmp)
		}
	}()

	w := bufio.NewWriter(f)
	for _, line := range lines {
		if err := line.write(w); err != nil {
			return err
		}
		if _, err := w.WriteString(newline); err != nil {
			return err
		}
	}
	if err := w.Flush(); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}

	for attempt := 1; ; attempt++ {
		err = os.Rename(tmp, path)
		if err == nil {
			break
		}
		if attempt >= renameAttempts {
			return fmt.Errorf("replace %s: %w", path, err)
		}
		time.Sleep(renameBackoff)
	}
	committed = true
	_ = os.Chmod(path, 0o600)
	return nil
}
