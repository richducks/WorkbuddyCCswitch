package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestChatStreamRetryOnCleanCloseBeforeOutput(t *testing.T) {
	withStreamTestAccount(t)
	oldClient := cfg.HttpClient
	t.Cleanup(func() { cfg.HttpClient = oldClient })

	for _, tc := range []struct {
		name       string
		first      string
		wantCalls  int
		wantOutput string
	}{
		{"no output", ": heartbeat\n\ndata: [DONE]\n\n", 2, "data: [DONE]"},
		{"still empty after retry", "", 2, "stream_closed_without_finish"},
		{"partial tool", "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"lookup\",\"arguments\":\"{\"}}]}}]}\n\n", 1, "stream_closed_without_finish"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			cfg.HttpClient = &http.Client{Transport: retryTransportFunc(func(req *http.Request) (*http.Response, error) {
				calls++
				body := tc.first
				if calls > 1 && tc.first != "" {
					body = "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ok\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
				}
				return &http.Response{
					StatusCode: http.StatusOK,
					Header:     make(http.Header),
					Body:       io.NopCloser(strings.NewReader(body)),
					Request:    req,
				}, nil
			})}
			recorder := httptest.NewRecorder()
			newAPIHandler().ServeHTTP(recorder, streamTestRequest())
			output := recorder.Body.String()
			if calls != tc.wantCalls || !strings.Contains(output, tc.wantOutput) {
				t.Fatalf("calls=%d want %d, output=%q want %q", calls, tc.wantCalls, output, tc.wantOutput)
			}
			if tc.wantOutput != "data: [DONE]" && strings.Contains(output, "data: [DONE]") {
				t.Fatalf("incomplete output reported success: %q", output)
			}
		})
	}
}

type canceledReadBody struct {
	ctx context.Context
}

func (body canceledReadBody) Read([]byte) (int, error) {
	<-body.ctx.Done()
	return 0, body.ctx.Err()
}

func (canceledReadBody) Close() error { return nil }

func TestChatStreamRetryDistinguishesIdleTimeoutFromCancellation(t *testing.T) {
	for _, tc := range []struct {
		name             string
		idle             time.Duration
		cancelBeforeRead bool
		want             error
	}{
		{"upstream idle timeout", time.Millisecond, false, context.DeadlineExceeded},
		{"caller canceled", time.Hour, true, context.Canceled},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			body := newIdleReadCloser(canceledReadBody{ctx: ctx}, tc.idle, cancel)
			defer body.Close()
			if tc.cancelBeforeRead {
				cancel()
			}
			_, err := body.Read(make([]byte, 1))
			if !errors.Is(err, tc.want) {
				t.Fatalf("read error = %v, want %v", err, tc.want)
			}
		})
	}
}

func TestChatStreamRetryAggregateReportsGatewayTimeout(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	body := newIdleReadCloser(canceledReadBody{ctx: ctx}, time.Millisecond, cancel)
	recorder := httptest.NewRecorder()
	writeChatAggregate(recorder, streamTestRequest(), &http.Response{Body: body}, "chosen", 1, nil, nil, time.Now())
	if recorder.Code != http.StatusGatewayTimeout || !strings.Contains(recorder.Body.String(), "upstream_idle_timeout") {
		t.Fatalf("status=%d body=%q", recorder.Code, recorder.Body.String())
	}
}
