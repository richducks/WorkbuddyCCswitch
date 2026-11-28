package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestResolvedModelAllResponsePaths(t *testing.T) {
	handlers := []struct {
		name string
		send func(http.ResponseWriter, *http.Request, *http.Response, time.Time)
	}{
		{"chat stream", func(w http.ResponseWriter, r *http.Request, resp *http.Response, start time.Time) {
			streamChatResponse(w, r, resp, "auto", 1, &Account{}, &profileCN, start)
		}},
		{"chat aggregate", func(w http.ResponseWriter, r *http.Request, resp *http.Response, start time.Time) {
			writeChatAggregate(w, r, resp, "auto", 1, &Account{}, &profileCN, start)
		}},
		{"responses stream", func(w http.ResponseWriter, r *http.Request, resp *http.Response, start time.Time) {
			streamResponsesResponse(w, r, resp, "auto", 1, &Account{}, &profileCN, start)
		}},
		{"responses aggregate", func(w http.ResponseWriter, r *http.Request, resp *http.Response, start time.Time) {
			writeResponsesAggregate(w, r, resp, "auto", 1, &Account{}, &profileCN, start)
		}},
		{"messages stream", func(w http.ResponseWriter, r *http.Request, resp *http.Response, start time.Time) {
			streamMessagesResponse(w, r, resp, "auto", 1, &Account{}, &profileCN, start, false)
		}},
		{"messages aggregate", func(w http.ResponseWriter, r *http.Request, resp *http.Response, start time.Time) {
			writeMessagesAggregate(w, r, resp, "auto", 1, &Account{}, &profileCN, start, false)
		}},
	}
	for _, handler := range handlers {
		for _, resolved := range []string{"", "upstream-model"} {
			t.Run(handler.name+"/"+resolved, func(t *testing.T) {
				isolatedGatewayConfig(t)
				resetStatsState(t)
				loadOptionalStats()
				start := time.Now()
				attempt := &statsAttempt{id: "fixture-account", model: "auto", started: start}
				req := httptest.NewRequest(http.MethodPost, "/", nil)
				req = req.WithContext(context.WithValue(req.Context(), statsRequestContextKey{}, attempt))
				modelField := ""
				if resolved != "" {
					modelField = `"model":"upstream-model",`
				}
				stream := "data: {" + modelField + `"choices":[{"delta":{"content":"ok"}}]}` + "\n\n" +
					"data: " + `{"model":"auto","choices":[{"delta":{},"finish_reason":"stop"}]}` + "\n\n" +
					"data: [DONE]\n\n"
				recorder := httptest.NewRecorder()
				handler.send(recorder, req, &http.Response{Body: io.NopCloser(strings.NewReader(stream))}, start)
				statsFinishAttempt(req, recorder.Code)
				requestStats.Lock()
				rows := append([]statsRequest(nil), requestStats.document.Requests...)
				requestStats.Unlock()
				if len(rows) != 1 || rows[0].Status != http.StatusOK || rows[0].Model != "auto" || rows[0].ResolvedModel != resolved {
					t.Fatalf("want requested auto and resolved %q, got %+v", resolved, rows)
				}
			})
		}
	}
}
