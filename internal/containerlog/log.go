package containerlog

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
)

const maxSize = 1 << 20
const backups = 3

// Open appends to the log files used by the operations page and mirrors output to stderr.
func Open(dataDir, name string) (io.WriteCloser, error) {
	dir := filepath.Join(dataDir, "logs")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	log := &writer{path: filepath.Join(dir, name+".log")}
	if err := log.open(); err != nil {
		return nil, err
	}
	return log, nil
}

type writer struct {
	path string
	file *os.File
	size int64
}

func (w *writer) open() error {
	file, err := os.OpenFile(w.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	info, err := file.Stat()
	if err != nil {
		file.Close()
		return err
	}
	w.file, w.size = file, info.Size()
	return nil
}

func (w *writer) Close() error {
	if w.file == nil {
		return nil
	}
	err := w.file.Close()
	w.file = nil
	return err
}

func (w *writer) rotate() error {
	if err := w.Close(); err != nil {
		return err
	}
	for i := backups; i > 0; i-- {
		previous := w.path
		if i > 1 {
			previous += "." + strconv.Itoa(i-1)
		}
		if err := os.Rename(previous, w.path+"."+strconv.Itoa(i)); err != nil && !errors.Is(err, os.ErrNotExist) {
			_ = w.open()
			return err
		}
	}
	return w.open()
}

// The standard logger serializes calls to Write.
func (w *writer) Write(message []byte) (int, error) {
	n, err := os.Stderr.Write(message)
	if w.file == nil {
		return n, err
	}
	if w.size+int64(len(message)) > maxSize {
		if rotateErr := w.rotate(); rotateErr != nil {
			fmt.Fprintln(os.Stderr, "日志滚动失败")
			return n, err
		}
	}
	written, fileErr := w.file.Write(message)
	w.size += int64(written)
	if fileErr != nil {
		fmt.Fprintln(os.Stderr, "日志文件写入失败")
	}
	return n, err
}
