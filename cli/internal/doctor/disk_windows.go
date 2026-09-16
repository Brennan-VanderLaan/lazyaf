//go:build windows

package doctor

import "golang.org/x/sys/windows"

// freeBytes is shutil.disk_usage(path).free via GetDiskFreeSpaceEx: the
// bytes available to the calling user on the volume holding path (§2.4).
func freeBytes(path string) (uint64, error) {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return 0, err
	}
	var available, total, free uint64
	if err := windows.GetDiskFreeSpaceEx(p, &available, &total, &free); err != nil {
		return 0, err
	}
	return available, nil
}
