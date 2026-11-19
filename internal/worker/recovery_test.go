package worker

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func TestCrashRecoversOnlyUnexhaustedClaims(t *testing.T) {
	config, db := testDatabase(t)
	configFile := filepath.Join(config.DataDir, "fixture.toml")
	toml := fmt.Sprintf("data_dir=%q\ncredentials_dir=%q\n", config.DataDir, config.CredentialsDir)
	if err := os.WriteFile(configFile, []byte(toml), 0o600); err != nil {
		t.Fatal(err)
	}
	execute(t, db, `INSERT INTO settings VALUES ('max_attempts','2'),('retry_delays','"1"')`)
	execute(t, db, `INSERT INTO deliveries(id,bot_id,payload,matches,created,updated)
		VALUES ('resume',1,'事件：resume','[]',1,1)`)

	probed := make(chan struct{})
	requests := make(chan string, 8)
	var sends atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != hook {
			t.Errorf("unexpected path %q", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		requests <- readHook(t, r).Content["text"]
		if sends.Add(1) == 1 {
			close(probed)
			<-r.Context().Done()
			return
		}
		fmt.Fprint(w, `{"code":0}`)
	}))
	t.Cleanup(server.Close)

	stop := workerProcess(t, configFile, server.URL)
	select {
	case <-probed:
	case <-time.After(10 * time.Second):
		stop(true)
		t.Fatal("first worker never reached the simulated webhook")
	}
	if scalar[string](t, db, "SELECT state FROM deliveries WHERE id='resume'") != "sending" {
		t.Fatal("worker sent a request before persisting its claim")
	}
	duplicate, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	if err := run(duplicate, config, localClients(server.URL)); err == nil {
		t.Fatal("a second worker acquired the live process lock")
	}
	execute(t, db, `INSERT INTO deliveries(id,bot_id,payload,matches,state,attempts,created,updated)
		VALUES ('exhausted',1,'must never be sent','[]','sending',2,1,1)`)
	stop(true)
	if scalar[string](t, db, "SELECT state FROM deliveries WHERE id='resume'") != "sending" {
		t.Fatal("the killed process did not leave an interrupted claim")
	}
	due := scalar[float64](t, db, "SELECT next_attempt FROM deliveries WHERE id='resume'")

	stop = workerProcess(t, configFile, server.URL)
	until(t, "resumed delivery after process exit", func() bool {
		return scalar[int](t, db, "SELECT count(*) FROM deliveries WHERE id='resume' AND state='sent' AND attempts=2 AND updated>=?", due) == 1 &&
			scalar[int](t, db, "SELECT count(*) FROM deliveries WHERE id='exhausted' AND state='failed' AND attempts=2") == 1
	})
	stop(false)
	if sends.Load() != 2 || <-requests != "事件：resume" || <-requests != "事件：resume" ||
		scalar[string](t, db, "SELECT value FROM runtime WHERE key='running'") != "0" {
		t.Fatal("interrupted delivery changed identity, exceeded its limit, or left a running heartbeat")
	}
}

func workerProcess(t *testing.T, configPath, receiver string) func(bool) {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	process := exec.Command(exe, "-test.run=^TestWorkerProcessHelper$")
	process.Env = append(os.Environ(), "RSS_WATCH_TEST_CHILD_CONFIG="+configPath, "RSS_WATCH_TEST_CHILD_RECEIVER="+receiver)
	var output bytes.Buffer
	process.Stdout, process.Stderr = &output, &output
	if err := process.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- process.Wait() }()
	stopped := false
	stop := func(force bool) {
		if stopped {
			return
		}
		stopped = true
		if force {
			_ = process.Process.Kill()
		} else {
			_ = process.Process.Signal(syscall.SIGTERM)
		}
		select {
		case err := <-done:
			if force {
				exit, ok := err.(*exec.ExitError)
				if !ok || exit.Sys().(syscall.WaitStatus).Signal() != syscall.SIGKILL {
					t.Errorf("expected child SIGKILL, got %v\n%s", err, output.String())
				}
			} else if err != nil {
				t.Errorf("worker process failed: %v\n%s", err, output.String())
			}
		case <-time.After(8 * time.Second):
			_ = process.Process.Kill()
			<-done
			t.Error("worker process did not exit")
		}
	}
	t.Cleanup(func() { stop(false) })
	return stop
}

func TestWorkerProcessHelper(t *testing.T) {
	path := os.Getenv("RSS_WATCH_TEST_CHILD_CONFIG")
	if path == "" {
		t.Skip("worker subprocess helper")
	}
	config, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	if err := run(ctx, config, localClients(os.Getenv("RSS_WATCH_TEST_CHILD_RECEIVER"))); err != nil {
		t.Fatal(err)
	}
}
