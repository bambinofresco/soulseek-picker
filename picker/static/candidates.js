// Keeps the candidates table current without redrawing it.
//
// The section carries data-poll (a JSON state URL) while anything can still change. Each
// poll returns the row ids and versions in display order; only rows whose version changed
// (or that are new) are fetched and swapped in. A row that is expanded, focused, or has an
// open confirm form is left alone until it is collapsed. Polling stops when the server
// reports nothing active (search done, matching and log prefetches settled, no downloads).
(function () {
  let timer = null;

  const section = () => document.getElementById("candidates");

  function busy(group) {
    return group.classList.contains("open")
      || group.contains(document.activeElement)
      || group.querySelector("form.confirm, details[open]") !== null;
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(poll, ms);
  }

  function reload(sec) {
    htmx.ajax("GET", `/album/${sec.dataset.rg}/candidates?${sec.dataset.qs}`, { target: sec, swap: "outerHTML" });
  }

  async function fetchRows(sec, ids) {
    if (!ids.length) return new Map();
    const resp = await fetch(`/album/${sec.dataset.rg}/candidates/rows?ids=${ids.join(",")}&${sec.dataset.qs}`);
    const tpl = document.createElement("template");
    tpl.innerHTML = `<table>${await resp.text()}</table>`;
    return new Map([...tpl.content.querySelectorAll("tbody.cand-group")].map((g) => [g.dataset.id, g]));
  }

  async function sync(sec, rows) {
    const table = sec.querySelector("table.candidates");
    if (!table) return;
    const current = new Map([...table.querySelectorAll("tbody.cand-group")].map((g) => [g.dataset.id, g]));
    const wanted = rows.map(([id, version]) => [String(id), version]);
    const need = [];
    for (const [id, version] of wanted) {
      const g = current.get(id);
      if (!g) need.push(id);
      else if (g.dataset.version !== version) {
        if (busy(g)) g.dataset.stale = "1"; // refreshed when collapsed
        else need.push(id);
      }
    }
    const fresh = await fetchRows(sec, need);
    if (section() !== sec) return; // the section was replaced meanwhile

    const keep = new Set(wanted.map(([id]) => id));
    current.forEach((g, id) => { if (!keep.has(id) && !busy(g)) g.remove(); });

    const anyBusy = [...current.values()].some((g) => g.isConnected && busy(g));
    let anchor = table.querySelector("thead");
    for (const [id] of wanted) {
      let g = current.get(id);
      const f = fresh.get(id);
      if (f) {
        if (g && g.isConnected) g.replaceWith(f);
        else if (anyBusy) table.appendChild(f); // don't reshuffle under an open row; new rows go last
        g = f;
        htmx.process(f);
      }
      if (!g || !g.isConnected) continue;
      if (!anyBusy && anchor.nextElementSibling !== g) anchor.after(g);
      anchor = g;
    }
  }

  async function poll() {
    const sec = section();
    if (!sec || !sec.dataset.poll) return;
    let state;
    try {
      state = await (await fetch(sec.dataset.poll, { headers: { Accept: "application/json" } })).json();
    } catch (e) {
      schedule(10000);
      return;
    }
    if (section() !== sec) return;
    if (state.phase !== sec.dataset.phase || String(state.settled) !== sec.dataset.settled) {
      reload(sec); // e.g. the search finished, or everything settled: render the table (and the recommendation) once
      return;
    }
    const status = document.getElementById("candidates-status");
    if (status) status.innerHTML = state.status_html;
    try {
      await sync(sec, state.rows);
    } catch (e) {
      schedule(10000);
      return;
    }
    if (state.active) schedule(state.interval);
    else delete sec.dataset.poll;
  }

  function startPolling(delay) {
    const sec = section();
    if (!sec) return;
    sec.dataset.poll = `/album/${sec.dataset.rg}/candidates/state?${sec.dataset.qs}`;
    schedule(delay);
  }

  async function refreshRow(group) {
    const sec = section();
    const fresh = await fetchRows(sec, [group.dataset.id]);
    const f = fresh.get(group.dataset.id);
    if (f && group.isConnected && !busy(group)) {
      group.replaceWith(f);
      htmx.process(f);
    }
  }

  // Expand / collapse a candidate by clicking its summary row.
  document.addEventListener("click", (event) => {
    const row = event.target.closest("tr.cand");
    if (!row || event.target.closest("button, a, select, input, details, form")) return;
    const group = row.closest("tbody.cand-group");
    const detail = group.querySelector("tr.detail");
    const open = detail.hasAttribute("hidden");
    detail.toggleAttribute("hidden", !open);
    group.classList.toggle("open", open);
    if (!open && group.dataset.stale) refreshRow(group);
  });

  // Something changed server-side (log fetch, download started): poll soon.
  document.body.addEventListener("candidates-changed", () => startPolling(300));
  // The set of rows changed (target edition pinned): redraw the section once.
  document.body.addEventListener("candidates-reload", () => { const sec = section(); if (sec) reload(sec); });

  htmx.onLoad((elt) => {
    const sec = elt.id === "candidates" ? elt : null;
    if (sec && sec.dataset.poll) schedule(1500);
  });
})();
