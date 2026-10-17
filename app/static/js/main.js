document.addEventListener("DOMContentLoaded", () => {

    // получаем URL для EventSource из data-атрибута
    const eventsUrlBase = document.body.dataset.eventsUrl || '/events/'; // базовый URL без run_id

    // перехват формы и SSE
    const form = document.getElementById("check-form");
    const resetButton = document.getElementById("reset-form-button");
    const runButton = document.getElementById("run-button");
    const clearLogsButton = document.getElementById("clear-logs-button");
    const resultsContainer = document.getElementById("results-container");
    const totalsSlots = ["run-totals-top", "run-totals-bottom"]
        .map((id) => document.getElementById(id))
        .filter(Boolean);

    let currentEventSource = null;
    // Per-run bookkeeping for the totals plaque: the counts come with
    // run_started, the progress is counted here as results arrive.
    let runState = null;

    // Captured once: this page may say "Run check" or "Run multi-geo check",
    // and the previous version re-enabled every page with the text "Run checks".
    const runButtonLabel = runButton ? runButton.textContent : "";

    // During a run all three buttons are locked, not just Run: clearing the log
    // directory or resetting the form mid-run used to be possible.
    const setBusy = (busy) => {
        [runButton, clearLogsButton, resetButton].forEach((button) => {
            if (button) { button.disabled = busy; }
        });
        if (runButton) {
            runButton.textContent = busy ? "Running..." : runButtonLabel;
        }
    };

    const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

    const formatDuration = (ms) => {
        const seconds = Math.max(0, Math.round((ms || 0) / 1000));
        return `${Math.floor(seconds / 60)} m ${String(seconds % 60).padStart(2, "0")} s`;
    };

    // "45 checks · 15 domains × 3 countries · 1 m 05 s"
    const renderTotals = (tail) => {
        if (!runState) { return; }
        const totals = runState.totals;
        const parts = [
            plural(totals.checks, "check", "checks"),
            `${plural(totals.domains, "domain", "domains")} × ${plural(totals.countries, "country", "countries")}`,
        ];
        if (totals.invalid) {
            parts.push(`${plural(totals.invalid, "row", "rows")} skipped`);
        }
        if (tail) { parts.push(tail); }
        const text = parts.join(" · ");
        totalsSlots.forEach((slot) => { slot.textContent = text; slot.hidden = false; });
    };

    const clearTotals = () => {
        runState = null;
        totalsSlots.forEach((slot) => { slot.textContent = ""; slot.hidden = true; });
    };

    if (form && resetButton) {
        form.addEventListener("reset", () => {
            // setTimeout(0) - the browser clears the fields after this handler
            // runs, so anything done synchronously here would be undone.
            setTimeout(() => {
                if (resultsContainer) {
                    resultsContainer.innerHTML = '';
                }
                clearTotals();

                const urlsTextarea = document.getElementById("urls");
                if (urlsTextarea) {
                    urlsTextarea.focus();
                }
            }, 0);
        });
    }

    // The row's identity is the task's position in the input, handed out by
    // run_started and carried by every later event. Addressing rows by index
    // (rather than by url+country, as before) is what keeps the output in the
    // order it was submitted while the pool finishes in whatever order it likes.
    const rowId = (runId, index) => `result-${runId}-${index}`;

    const renderRow = (payload, phase) => {
        const card = document.createElement("div");
        card.className = "result-card";
        card.id = rowId(payload.run_id, payload.index);

        let icon = "⏳";
        let statusClass = "";
        let statusText = "Pending";
        let details = "—";
        let screenshotHtml = '<div class="screenshot-preview"></div>';

        if (phase === 'running') {
            icon = "🔄";
            statusText = "Running...";
            details = "...";
        } else if (phase === 'finished') {
            statusText = payload.result;
            if (payload.result === 'success') {
                icon = "✅"; statusClass = "status-success";
                details = `HTTP ${payload.http_code || 200} | TTFB: ${payload.ttfb_ms || '-'} ms`;
            } else {
                icon = "❌"; statusClass = "status-error";
                details = `Error: ${payload.result} ${payload.http_code ? `(${payload.http_code})` : ''} | ${payload.notes || ''}`;
            }
            if (payload.png_name) {
                const imageUrl = `/logs/${payload.png_name}`;
                screenshotHtml = `
                    <div class="screenshot-preview">
                        <a href="${imageUrl}" target="_blank" title="View full screenshot">
                            <img src="${imageUrl}" alt="Screenshot preview">
                        </a>
                    </div>`;
            } else {
                 screenshotHtml = '<div class="screenshot-preview muted">(no screenshot)</div>';
            }
        } else if (payload.parsing_error) {
            details = payload.parsing_error;
        }

        card.innerHTML = `
            <div class="status-icon">${icon}</div>
            <div> 
                <strong>${payload.url}</strong> 
                ${payload.country ? `<span class="code" style="margin-left: 8px; color: var(--c-text-muted); font-size: 0.9em;">[${payload.country.toUpperCase()}]</span>` : ''}
                <div class="timings">${details}</div> 
            </div>
            <div class="status ${statusClass}">${statusText}</div>
            ${screenshotHtml}`;
        return card;
     };

    const replaceRow = (payload, phase) => {
        const existing = document.getElementById(rowId(payload.run_id, payload.index));
        const card = renderRow(payload, phase);
        if (existing) {
            existing.replaceWith(card);
        } else {
            // Should not happen now that run_started draws every row, but a row
            // arriving late is still worth showing.
            resultsContainer.append(card);
        }
    };

    const renderHeader = (payload) => {
        const header = document.createElement("div");
        header.className = "result-header";
        const country = payload.settings?.country?.toUpperCase() || 'N/A';
        const checks = payload.totals?.checks ?? '?';
        header.innerHTML = `
            <strong>Run: <code>${payload.run_id}</code></strong>
            (${country}, ${plural(checks, 'check', 'checks')})
            <span id="run-status-${payload.run_id}">(Running...)</span>`;
        return header;
     };

    const renderRun = (payload) => {
        runState = { runId: payload.run_id, totals: payload.totals, done: 0 };
        resultsContainer.innerHTML = "";
        resultsContainer.append(renderHeader(payload));

        // One insertion for the whole run: an ALL-mode run is hundreds of rows,
        // and appending them one by one would reflow the page hundreds of times.
        const fragment = document.createDocumentFragment();
        (payload.tasks || []).forEach((task) => {
            fragment.append(renderRow({ run_id: payload.run_id, ...task }, 'pending'));
        });
        resultsContainer.append(fragment);

        renderTotals(`0/${payload.totals.checks} done`);
    };

    if (form && runButton) {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();

        // Сброс состояния интерфейса
        setBusy(true);
        resultsContainer.innerHTML = "";
        clearTotals();
        if (currentEventSource) { currentEventSource.close(); }

        const formData = new FormData(form);
        const allStatusMsg = document.getElementById("all-status-message");
        const selectedCountry = formData.get("country");

        if (allStatusMsg) {
            allStatusMsg.style.display = (selectedCountry === "all") ? "block" : "none";
        }

        let response;
        try {
          response = await fetch(form.action, { method: "POST", body: formData });
        } catch (err) {
          console.error("Fetch error:", err);
          resultsContainer.innerHTML = `<div class="result-message status-error">Network error submitting run.</div>`;
          setBusy(false); return;
        }

        if (!response.ok) {
          let errorMsg = 'Unknown error';
          try { const errData = await response.json(); errorMsg = errData.error || errorMsg; }
          catch(jsonErr) { errorMsg = await response.text(); }
          resultsContainer.innerHTML = `<div class="result-message status-error">Error ${response.status}: ${errorMsg}</div>`;
          setBusy(false); return;
        }

        const data = await response.json();
        const runId = data.run_id;

        // динамически собираем URL для EventSource
        const eventSourceUrl = `${eventsUrlBase}${runId}`;
        currentEventSource = new EventSource(eventSourceUrl);

        currentEventSource.onmessage = (event) => {
            const payload = JSON.parse(event.data);
            if (payload.type === 'run_started') {
                renderRun(payload);
            } else if (payload.type === 'check_started') {
                replaceRow(payload, 'running');
            } else if (payload.type === 'check_finished') {
                replaceRow(payload, 'finished');
                if (runState) {
                    runState.done += 1;
                    renderTotals(`${runState.done}/${runState.totals.checks} done`);
                }
            } else if (payload.type === 'run_finished') {
                // Обновляем статус в заголовке результатов
                const statusEl = document.getElementById(`run-status-${payload.run_id}`);
                if (statusEl) {
                    statusEl.textContent = `(Finished. OK: ${payload.totals.ok}, Err: ${payload.totals.err})`;
                }
                renderTotals(formatDuration(payload.totals.time_ms));
                if (runState) { runState.finished = true; }

                setBusy(false);
                if (allStatusMsg) {
                    allStatusMsg.style.display = "none";
                }
                currentEventSource.close();
            } else if (payload.type === 'stream_unavailable') {
                currentEventSource.close();
                setBusy(false);
                if (runState) {
                    // We already have this run on screen. The server closes the
                    // stream after the final event and EventSource reconnects on
                    // its own, so this is that reconnect finding the run gone -
                    // not a failure, and certainly not a reason to wipe results.
                    console.debug("SSE stream closed for a run already rendered:", payload.run_id);
                    return;
                }
                // Nothing was ever rendered: the run is executing somewhere this
                // stream cannot see - another gunicorn worker (see дефект 2).
                resultsContainer.innerHTML =
                    `<div class="result-message status-error">Live updates unavailable: ${payload.reason}</div>`;
                clearTotals();
            }
         };
        currentEventSource.onerror = (err) => {
             console.error("EventSource failed:", err);
             setBusy(false);
             if (currentEventSource) currentEventSource.close();
         };
      });
    }

    // Обработка кнопки "Clear logs"
    // Получаем URL из data-атрибута
    const clearLogsUrl = clearLogsButton?.dataset.clearUrl || '/logs/clear';

    if (clearLogsButton) {
        clearLogsButton.addEventListener("click", async () => {
            if (!confirm("Are you sure you want to delete all log files? This cannot be undone.")) { return; }
            try {
                // используем URL из clearLogsUrl
                const response = await fetch(clearLogsUrl, { method: "POST" });
                if (response.ok) {
                    alert("Logs cleared successfully.");
                    resultsContainer.innerHTML = '<p class="muted mt-2">Logs cleared.</p>';
                    clearTotals();
                } else {
                    const errData = await response.json();
                    alert(`Error clearing logs: ${errData.message || 'Unknown error'}`);
                }
            } catch (err) {
                console.error("Clear logs fetch error:", err);
                alert("Network error while trying to clear logs.");
            }
        });
    }
  });
