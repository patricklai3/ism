// ISM Shop OS — Technician Assignment Matrix & Rebalance Preview Modals (Vanilla ES Module)
// Consumes ism.api.technician_matrix.* endpoints.

const WEEK_DAYS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

const DIFFICULTY_META = {
  0: { label: "—", name: "Blocked", cls: "cockpit-cell-badge--d0" },
  1: { label: "L1", name: "Routine", cls: "cockpit-cell-badge--d1" },
  2: { label: "L2", name: "Standard", cls: "cockpit-cell-badge--d2" },
  3: { label: "L3", name: "Complex", cls: "cockpit-cell-badge--d3" },
};

const ROLE_META = {
  Preferred: { short: "★ Pref", cls: "cockpit-cell-role--pref" },
  Standard: { short: "Std", cls: "cockpit-cell-role--std" },
  Backup: { short: "Bkp", cls: "cockpit-cell-role--bkp" },
};

function toTimeInput(val) {
  if (!val) return "";
  const parts = String(val).trim().split(":");
  if (parts.length >= 2) {
    return `${parts[0].padStart(2, "0")}:${parts[1].padStart(2, "0")}`;
  }
  return "";
}

function toServerTime(val) {
  const s = String(val || "").trim();
  if (!s) return null;
  return s.length === 5 ? `${s}:00` : s;
}

export function initMatrixModals({
  apiGet,
  apiPost,
  esc,
  showToast,
  showError,
  getSelectedDate,
  onDashboardRefreshNeeded,
}) {
  const matrixState = {
    data: null,
    expandedShiftTechs: new Set(),
    activePopover: null, // { employee, skill }
    saving: false,
  };

  const rebalanceState = {
    preview: null,
    applying: false,
  };

  function getModalEl(id) {
    return document.getElementById(id);
  }

  function openModal(id) {
    const el = getModalEl(id);
    if (!el) return;
    el.hidden = false;
    document.body.classList.add("cockpit-modal-open");
  }

  function closeModal(id) {
    const el = getModalEl(id);
    if (!el) return;
    el.hidden = true;
    if (id === "matrixModal") {
      matrixState.activePopover = null;
    }
    if (!document.querySelector(".cockpit-modal:not([hidden])")) {
      document.body.classList.remove("cockpit-modal-open");
    }
  }

  function closeAllModals() {
    closeModal("matrixModal");
    closeModal("rebalanceModal");
  }

  function renderCellBadgeHtml(cell) {
    const diff = Number(cell && cell.max_difficulty != null ? cell.max_difficulty : 0);
    const role = (cell && cell.skill_role) || "Standard";
    const dMeta = DIFFICULTY_META[diff] || DIFFICULTY_META[0];
    const rMeta = ROLE_META[role] || ROLE_META.Standard;
    const roleHtml =
      diff > 0
        ? `<span class="cockpit-cell-role ${rMeta.cls}">${esc(rMeta.short)}</span>`
        : "";
    return (
      `<span class="cockpit-cell-badge ${dMeta.cls}">${esc(dMeta.label)}</span>` +
      roleHtml
    );
  }

  function renderShiftEditorHtml(tech) {
    const byDay = new Map();
    for (const row of Array.isArray(tech.weekly_schedule) ? tech.weekly_schedule : []) {
      if (row && row.day_of_week) byDay.set(row.day_of_week, row);
    }

    const dayRows = WEEK_DAYS.map((day) => {
      const row = byDay.get(day) || {
        day_of_week: day,
        is_working_day: day === "Sunday" ? 0 : 1,
        shift_start: "08:00:00",
        shift_end: "17:00:00",
      };
      const working = Boolean(Number(row.is_working_day));
      const startVal = toTimeInput(row.shift_start || "08:00:00");
      const endVal = toTimeInput(row.shift_end || "17:00:00");
      return (
        `<div class="cockpit-shift-day" data-day="${esc(day)}">` +
        `<label class="cockpit-shift-check">` +
        `<input type="checkbox" class="js-shift-working" ${working ? "checked" : ""}>` +
        `<span>${esc(day.slice(0, 3))}</span>` +
        `</label>` +
        `<input type="time" class="cockpit-input cockpit-input--xs js-shift-start" value="${esc(startVal)}" ${working ? "" : "disabled"}>` +
        `<span class="cockpit-shift-sep">–</span>` +
        `<input type="time" class="cockpit-input cockpit-input--xs js-shift-end" value="${esc(endVal)}" ${working ? "" : "disabled"}>` +
        `</div>`
      );
    }).join("");

    return (
      `<div class="cockpit-shift-editor" data-employee="${esc(tech.employee)}">` +
      `<div class="cockpit-shift-editor__head">` +
      `<strong>Weekly Shift Schedule — ${esc(tech.employee_name || tech.employee)}</strong>` +
      `<button type="button" class="cockpit-btn cockpit-btn--sm cockpit-btn--primary js-save-shifts" data-employee="${esc(tech.employee)}">Save Shifts</button>` +
      `</div>` +
      `<div class="cockpit-shift-grid">${dayRows}</div>` +
      `</div>`
    );
  }

  function renderMatrixPopoverHtml() {
    const pop = matrixState.activePopover;
    if (!pop || !matrixState.data) return "";
    const tech = (matrixState.data.technicians || []).find((t) => t.employee === pop.employee);
    if (!tech) return "";
    const cell = (tech.cells && tech.cells[pop.skill]) || { max_difficulty: 0, skill_role: "Standard" };
    const curDiff = Number(cell.max_difficulty || 0);
    const curRole = cell.skill_role || "Standard";

    const diffBtns = [0, 1, 2, 3]
      .map((d) => {
        const meta = DIFFICULTY_META[d];
        const active = curDiff === d ? "is-selected" : "";
        return `<button type="button" class="cockpit-pop-chip js-pop-diff ${active}" data-diff="${d}">${esc(d === 0 ? "0 Blocked" : `${meta.label} ${meta.name}`)}</button>`;
      })
      .join("");

    const roleBtns = ["Preferred", "Standard", "Backup"]
      .map((r) => {
        const active = curRole === r ? "is-selected" : "";
        return `<button type="button" class="cockpit-pop-chip js-pop-role ${active}" data-role="${esc(r)}">${esc(r)}</button>`;
      })
      .join("");

    return (
      `<div class="cockpit-matrix-popover" id="matrixCellPopover" role="dialog" aria-label="Edit skill cell">` +
      `<div class="cockpit-matrix-popover__head">` +
      `<span><strong>${esc(tech.employee_name || tech.employee)}</strong> · ${esc(pop.skill)}</span>` +
      `<button type="button" class="cockpit-btn cockpit-btn--sm cockpit-btn--subtle js-close-popover" aria-label="Close cell editor">&times;</button>` +
      `</div>` +
      `<div class="cockpit-matrix-popover__group">` +
      `<div class="cockpit-matrix-popover__label">Max Difficulty</div>` +
      `<div class="cockpit-matrix-popover__options">${diffBtns}</div>` +
      `</div>` +
      `<div class="cockpit-matrix-popover__group">` +
      `<div class="cockpit-matrix-popover__label">Skill Role</div>` +
      `<div class="cockpit-matrix-popover__options">${roleBtns}</div>` +
      `</div>` +
      `</div>`
    );
  }

  function renderMatrixModalBody() {
    const body = getModalEl("matrixModalBody");
    const dateBadge = getModalEl("matrixModalDateBadge");
    if (!body) return;

    const data = matrixState.data;
    if (!data) {
      body.innerHTML = `<div class="cockpit-empty-state cockpit-empty-state--neutral">Loading technician assignment matrix…</div>`;
      return;
    }

    if (dateBadge) {
      dateBadge.textContent = data.date || getSelectedDate() || "Today";
    }

    const policy = data.policy || {};
    const skills = Array.isArray(data.skills) ? data.skills : [];
    const technicians = Array.isArray(data.technicians) ? data.technicians : [];

    const modeOptions = ["Tiered Waterfall", "Equal Balance", "Weighted Share"]
      .map((m) => `<option value="${esc(m)}" ${policy.assignment_policy_mode === m ? "selected" : ""}>${esc(m)}</option>`)
      .join("");

    const policyBarHtml =
      `<form id="matrixPolicyForm" class="cockpit-policy-bar">` +
      `<label class="cockpit-policy-field">` +
      `<span>Policy Mode</span>` +
      `<select id="policyModeSelect" class="cockpit-select">${modeOptions}</select>` +
      `</label>` +
      `<label class="cockpit-policy-field">` +
      `<span>Spillover %</span>` +
      `<input id="policySpilloverInput" class="cockpit-input cockpit-input--sm" type="number" min="10" max="100" step="1" value="${esc(policy.tier_spillover_threshold_pct ?? 85)}">` +
      `</label>` +
      `<label class="cockpit-policy-field">` +
      `<span>Fairness Days</span>` +
      `<input id="policyWindowInput" class="cockpit-input cockpit-input--sm" type="number" min="1" max="30" step="1" value="${esc(policy.fairness_window_days ?? 7)}">` +
      `</label>` +
      `<label class="cockpit-policy-check">` +
      `<input id="policyAutoRebalanceCheck" type="checkbox" ${Number(policy.enable_morning_auto_rebalance) ? "checked" : ""}>` +
      `<span>Morning Auto-Rebalance</span>` +
      `</label>` +
      `<button type="submit" id="savePolicyBtn" class="cockpit-btn cockpit-btn--primary cockpit-btn--sm">Save Policy</button>` +
      `</form>`;

    const skillHeadersHtml = skills
      .map((s) => {
        const w = Number(s.scarcity_weight || 1.0);
        const wTag = w !== 1.0 ? `<span class="cockpit-matrix-scarcity">×${esc(w.toFixed(1))}</span>` : "";
        return `<th scope="col" title="${esc(s.description || s.name)}"><div class="cockpit-matrix-skill-hd">${esc(s.name)}${wTag}</div></th>`;
      })
      .join("");

    const rowsHtml = technicians.length
      ? technicians
          .map((tech) => {
            const fairness = tech.fairness || {};
            const loadPct = Math.round(Number(fairness.normalized_load || 0) * 100);
            const frtHrs = Number(fairness.assigned_frt_hours || 0).toFixed(1);
            const tierVal = Number(tech.assignment_tier || 1);
            const weightVal = Number(tech.target_load_weight ?? 1.0);
            const isExpanded = matrixState.expandedShiftTechs.has(tech.employee);

            const tierSelectHtml =
              `<select class="cockpit-select cockpit-select--xs js-tech-tier" data-employee="${esc(tech.employee)}" aria-label="Tier for ${esc(tech.employee_name || tech.employee)}">` +
              `<option value="1" ${tierVal === 1 ? "selected" : ""}>T1 Workhorse</option>` +
              `<option value="2" ${tierVal === 2 ? "selected" : ""}>T2 Buffer</option>` +
              `<option value="3" ${tierVal === 3 ? "selected" : ""}>T3 Reserve</option>` +
              `</select>`;

            const weightInputHtml =
              `<input type="number" class="cockpit-input cockpit-input--xs js-tech-weight" data-employee="${esc(tech.employee)}" ` +
              `min="0.1" max="5" step="0.1" value="${esc(weightVal)}" title="Target load weight" aria-label="Load weight for ${esc(tech.employee_name || tech.employee)}">`;

            const cellsHtml = skills
              .map((s) => {
                const cell = (tech.cells && tech.cells[s.name]) || { max_difficulty: 0, skill_role: "Standard" };
                const isActive =
                  matrixState.activePopover &&
                  matrixState.activePopover.employee === tech.employee &&
                  matrixState.activePopover.skill === s.name;
                return (
                  `<td class="cockpit-matrix-td">` +
                  `<button type="button" class="cockpit-matrix-cell-btn js-matrix-cell ${isActive ? "is-editing" : ""}" ` +
                  `data-employee="${esc(tech.employee)}" data-skill="${esc(s.name)}" ` +
                  `title="Edit ${esc(tech.employee_name || tech.employee)} — ${esc(s.name)}">` +
                  renderCellBadgeHtml(cell) +
                  `</button>` +
                  `</td>`
                );
              })
              .join("");

            const shiftRowHtml = isExpanded
              ? `<tr class="cockpit-matrix-shift-row"><td colspan="${skills.length + 1}">${renderShiftEditorHtml(tech)}</td></tr>`
              : "";

            return (
              `<tr>` +
              `<th scope="row" class="cockpit-matrix-tech-cell">` +
              `<div class="cockpit-matrix-tech-top">` +
              `<span class="cockpit-matrix-tech-name">${esc(tech.employee_name || tech.employee)}</span>` +
              `<span class="cockpit-pill cockpit-pill--sky cockpit-pill--xs" title="Rolling fairness load">${esc(loadPct)}% · ${esc(frtHrs)}h</span>` +
              `</div>` +
              `<div class="cockpit-matrix-tech-controls">` +
              tierSelectHtml +
              weightInputHtml +
              `<button type="button" class="cockpit-btn cockpit-btn--sm js-toggle-shifts ${isExpanded ? "cockpit-btn--primary" : ""}" data-employee="${esc(tech.employee)}">Shifts</button>` +
              `</div>` +
              `</th>` +
              cellsHtml +
              `</tr>` +
              shiftRowHtml
            );
          })
          .join("")
      : `<tr><td colspan="${Math.max(2, skills.length + 1)}"><div class="cockpit-empty-state cockpit-empty-state--neutral">No active technician profiles configured.</div></td></tr>`;

    body.innerHTML =
      policyBarHtml +
      renderMatrixPopoverHtml() +
      `<div class="cockpit-matrix-wrap">` +
      `<table class="cockpit-matrix-table">` +
      `<thead><tr><th scope="col" class="cockpit-matrix-tech-hd">Technician</th>${skillHeadersHtml}</tr></thead>` +
      `<tbody>${rowsHtml}</tbody>` +
      `</table>` +
      `</div>`;
  }

  async function openMatrixModal() {
    openModal("matrixModal");
    matrixState.data = null;
    matrixState.activePopover = null;
    renderMatrixModalBody();
    try {
      const targetDate = getSelectedDate();
      const res = await apiGet("ism.api.technician_matrix.get_assignment_matrix", {
        target_date: targetDate,
      });
      matrixState.data = res;
      renderMatrixModalBody();
    } catch (err) {
      const body = getModalEl("matrixModalBody");
      if (body) {
        body.innerHTML = `<div class="cockpit-banner cockpit-banner--error">${esc(err.message || "Failed to load matrix.")}</div>`;
      }
      showError(err.message || "Failed to load technician matrix.");
    }
  }

  async function saveMatrixCell(employee, skill, maxDifficulty, skillRole) {
    if (!matrixState.data) return;
    const tech = (matrixState.data.technicians || []).find((t) => t.employee === employee);
    const prev = tech && tech.cells ? { ...(tech.cells[skill] || { max_difficulty: 0, skill_role: "Standard" }) } : null;

    if (tech) {
      tech.cells = tech.cells || {};
      tech.cells[skill] = { max_difficulty: maxDifficulty, skill_role: skillRole };
      renderMatrixModalBody();
    }

    try {
      await apiPost("ism.api.technician_matrix.save_matrix_cell", {
        employee,
        skill,
        max_difficulty: maxDifficulty,
        skill_role: skillRole,
      });
      showToast(`Updated ${tech ? tech.employee_name || employee : employee} · ${skill}`);
    } catch (err) {
      if (tech && prev) {
        tech.cells[skill] = prev;
        renderMatrixModalBody();
      }
      showError(err.message || "Failed to save matrix cell.");
    }
  }

  function renderRebalanceModalBody() {
    const body = getModalEl("rebalanceModalBody");
    const dateBadge = getModalEl("rebalanceModalDateBadge");
    if (!body) return;

    const data = rebalanceState.preview;
    if (!data) {
      body.innerHTML = `<div class="cockpit-empty-state cockpit-empty-state--neutral">Analyzing schedule load &amp; unlocked entries…</div>`;
      return;
    }

    if (dateBadge) {
      dateBadge.textContent = data.date || getSelectedDate() || "Today";
    }

    const beforePct = Number(data.before_spread_pct || 0).toFixed(1);
    const afterPct = Number(data.after_spread_pct || 0).toFixed(1);
    const lockedCount = Number(data.locked_count || 0);
    const unlockedCount = Number(data.unlocked_count || 0);
    const changes = Array.isArray(data.changes) ? data.changes : [];

    const summaryHtml =
      `<div class="cockpit-rebalance-summary">` +
      `<div class="cockpit-rebalance-stat">` +
      `<span class="cockpit-rebalance-stat__label">Load Spread</span>` +
      `<span class="cockpit-rebalance-stat__val">${esc(beforePct)}% &rarr; ${esc(afterPct)}%</span>` +
      `</div>` +
      `<div class="cockpit-rebalance-stat">` +
      `<span class="cockpit-rebalance-stat__label">Entries</span>` +
      `<span class="cockpit-rebalance-stat__val">${esc(lockedCount)} Locked · ${esc(unlockedCount)} Unlocked</span>` +
      `</div>` +
      `</div>`;

    if (!changes.length) {
      body.innerHTML =
        summaryHtml +
        `<div class="cockpit-empty-state">` +
        `<strong>Already Balanced</strong><br>` +
        `No beneficial technician swaps found among unlocked entries for ${esc(data.date || "this date")}.` +
        `</div>` +
        `<div class="cockpit-modal__foot">` +
        `<button type="button" class="cockpit-btn" data-close-modal="rebalanceModal">Close</button>` +
        `</div>`;
      return;
    }

    const rowsHtml = changes
      .map(
        (c) =>
          `<tr>` +
          `<td><code>${esc(c.scheduled_time || "—")}</code></td>` +
          `<td><strong>${esc(c.schedule_entry || "—")}</strong>${c.repair_order ? `<div class="cockpit-rebalance-ro">${esc(c.repair_order)}</div>` : ""}</td>` +
          `<td><span class="cockpit-pill cockpit-pill--muted">${esc(c.from_tech || "Unassigned")}</span> &rarr; <span class="cockpit-pill cockpit-pill--green">${esc(c.to_tech || "—")}</span></td>` +
          `<td>${esc(c.reason || "")}</td>` +
          `</tr>`
      )
      .join("");

    body.innerHTML =
      summaryHtml +
      `<div class="cockpit-matrix-wrap">` +
      `<table class="cockpit-rebalance-table">` +
      `<thead><tr><th>Time</th><th>Entry / RO</th><th>From &rarr; To</th><th>Reason</th></tr></thead>` +
      `<tbody>${rowsHtml}</tbody>` +
      `</table>` +
      `</div>` +
      `<div class="cockpit-modal__foot">` +
      `<button type="button" class="cockpit-btn" data-close-modal="rebalanceModal">Cancel</button>` +
      `<button type="button" id="confirmRebalanceBtn" class="cockpit-btn cockpit-btn--primary" ${rebalanceState.applying ? "disabled" : ""}>` +
      `${rebalanceState.applying ? "Applying…" : `Confirm Rebalance (${changes.length})`}` +
      `</button>` +
      `</div>`;
  }

  async function openRebalanceModal() {
    openModal("rebalanceModal");
    rebalanceState.preview = null;
    rebalanceState.applying = false;
    renderRebalanceModalBody();
    try {
      const targetDate = getSelectedDate();
      const res = await apiGet("ism.api.technician_matrix.preview_rebalance", {
        target_date: targetDate,
      });
      rebalanceState.preview = res;
      renderRebalanceModalBody();
    } catch (err) {
      const body = getModalEl("rebalanceModalBody");
      if (body) {
        body.innerHTML = `<div class="cockpit-banner cockpit-banner--error">${esc(err.message || "Failed to preview rebalance.")}</div>`;
      }
      showError(err.message || "Failed to preview rebalance.");
    }
  }

  // Event delegation for modals
  document.addEventListener("click", async (ev) => {
    const closeTrigger = ev.target.closest("[data-close-modal]");
    if (closeTrigger) {
      closeModal(closeTrigger.getAttribute("data-close-modal"));
      return;
    }

    const matrixBody = getModalEl("matrixModalBody");
    if (matrixBody && matrixBody.contains(ev.target)) {
      if (ev.target.closest(".js-close-popover")) {
        matrixState.activePopover = null;
        renderMatrixModalBody();
        return;
      }

      const cellBtn = ev.target.closest(".js-matrix-cell");
      if (cellBtn) {
        const employee = cellBtn.getAttribute("data-employee");
        const skill = cellBtn.getAttribute("data-skill");
        if (
          matrixState.activePopover &&
          matrixState.activePopover.employee === employee &&
          matrixState.activePopover.skill === skill
        ) {
          matrixState.activePopover = null;
        } else {
          matrixState.activePopover = { employee, skill };
        }
        renderMatrixModalBody();
        return;
      }

      const diffBtn = ev.target.closest(".js-pop-diff");
      if (diffBtn && matrixState.activePopover && matrixState.data) {
        const { employee, skill } = matrixState.activePopover;
        const tech = (matrixState.data.technicians || []).find((t) => t.employee === employee);
        const curRole = (tech && tech.cells && tech.cells[skill] && tech.cells[skill].skill_role) || "Standard";
        const newDiff = Number(diffBtn.getAttribute("data-diff") || 0);
        await saveMatrixCell(employee, skill, newDiff, curRole);
        return;
      }

      const roleBtn = ev.target.closest(".js-pop-role");
      if (roleBtn && matrixState.activePopover && matrixState.data) {
        const { employee, skill } = matrixState.activePopover;
        const tech = (matrixState.data.technicians || []).find((t) => t.employee === employee);
        const curDiff = Number(
          tech && tech.cells && tech.cells[skill] && tech.cells[skill].max_difficulty != null
            ? tech.cells[skill].max_difficulty
            : 1
        );
        const newRole = roleBtn.getAttribute("data-role") || "Standard";
        await saveMatrixCell(employee, skill, curDiff, newRole);
        return;
      }

      const shiftToggleBtn = ev.target.closest(".js-toggle-shifts");
      if (shiftToggleBtn) {
        const emp = shiftToggleBtn.getAttribute("data-employee");
        if (matrixState.expandedShiftTechs.has(emp)) {
          matrixState.expandedShiftTechs.delete(emp);
        } else {
          matrixState.expandedShiftTechs.add(emp);
        }
        renderMatrixModalBody();
        return;
      }

      const saveShiftsBtn = ev.target.closest(".js-save-shifts");
      if (saveShiftsBtn) {
        const emp = saveShiftsBtn.getAttribute("data-employee");
        const editor = matrixBody.querySelector(`.cockpit-shift-editor[data-employee="${CSS.escape(emp)}"]`);
        if (!editor) return;
        const weeklySchedule = Array.from(editor.querySelectorAll(".cockpit-shift-day")).map((dayEl) => {
          const day = dayEl.getAttribute("data-day");
          const isWorking = dayEl.querySelector(".js-shift-working")?.checked ? 1 : 0;
          const start = toServerTime(dayEl.querySelector(".js-shift-start")?.value) || "08:00:00";
          const end = toServerTime(dayEl.querySelector(".js-shift-end")?.value) || "17:00:00";
          return {
            day_of_week: day,
            is_working_day: isWorking,
            shift_start: start,
            shift_end: end,
          };
        });
        saveShiftsBtn.disabled = true;
        try {
          await apiPost("ism.api.technician_matrix.update_technician_profile", {
            employee: emp,
            weekly_schedule: weeklySchedule,
          });
          const tech = (matrixState.data?.technicians || []).find((t) => t.employee === emp);
          if (tech) tech.weekly_schedule = weeklySchedule;
          showToast(`Saved weekly shifts for ${tech ? tech.employee_name || emp : emp}.`);
          onDashboardRefreshNeeded();
        } catch (err) {
          showError(err.message || "Failed to save weekly shifts.");
        } finally {
          saveShiftsBtn.disabled = false;
        }
        return;
      }
    }

    const confirmRebalBtn = ev.target.closest("#confirmRebalanceBtn");
    if (confirmRebalBtn && rebalanceState.preview && !rebalanceState.applying) {
      const changes = Array.isArray(rebalanceState.preview.changes) ? rebalanceState.preview.changes : [];
      if (!changes.length) return;
      rebalanceState.applying = true;
      renderRebalanceModalBody();
      try {
        await apiPost("ism.api.technician_matrix.apply_rebalance", {
          target_date: rebalanceState.preview.date || getSelectedDate(),
          entry_names: changes.map((c) => c.schedule_entry),
        });
        showToast(`Rebalanced ${changes.length} schedule ${changes.length === 1 ? "entry" : "entries"}.`);
        closeModal("rebalanceModal");
        onDashboardRefreshNeeded();
      } catch (err) {
        rebalanceState.applying = false;
        renderRebalanceModalBody();
        showError(err.message || "Failed to apply rebalance.");
      }
    }
  });

  document.addEventListener("change", async (ev) => {
    const matrixBody = getModalEl("matrixModalBody");
    if (!matrixBody || !matrixBody.contains(ev.target)) return;

    if (ev.target.classList.contains("js-shift-working")) {
      const dayEl = ev.target.closest(".cockpit-shift-day");
      if (dayEl) {
        const disabled = !ev.target.checked;
        dayEl.querySelectorAll("input[type='time']").forEach((inp) => {
          inp.disabled = disabled;
        });
      }
      return;
    }

    if (ev.target.classList.contains("js-tech-tier") || ev.target.classList.contains("js-tech-weight")) {
      const emp = ev.target.getAttribute("data-employee");
      const tech = (matrixState.data?.technicians || []).find((t) => t.employee === emp);
      if (!tech) return;
      const row = ev.target.closest("tr");
      const tier = Number(row?.querySelector(".js-tech-tier")?.value || tech.assignment_tier || 1);
      const weight = Number(row?.querySelector(".js-tech-weight")?.value || tech.target_load_weight || 1.0);
      try {
        await apiPost("ism.api.technician_matrix.update_technician_profile", {
          employee: emp,
          assignment_tier: tier,
          target_load_weight: weight > 0 ? weight : 1.0,
        });
        tech.assignment_tier = tier;
        tech.target_load_weight = weight > 0 ? weight : 1.0;
        showToast(`Updated ${tech.employee_name || emp} profile.`);
        onDashboardRefreshNeeded();
      } catch (err) {
        showError(err.message || "Failed to update technician profile.");
        renderMatrixModalBody();
      }
    }
  });

  document.addEventListener("submit", async (ev) => {
    if (ev.target && ev.target.id === "matrixPolicyForm") {
      ev.preventDefault();
      const mode = document.getElementById("policyModeSelect")?.value || "Tiered Waterfall";
      const spillover = Number(document.getElementById("policySpilloverInput")?.value ?? 85);
      const windowDays = Number(document.getElementById("policyWindowInput")?.value ?? 7);
      const autoRebal = document.getElementById("policyAutoRebalanceCheck")?.checked ? 1 : 0;
      const btn = document.getElementById("savePolicyBtn");
      if (btn) btn.disabled = true;
      try {
        await apiPost("ism.api.technician_matrix.save_assignment_policy", {
          assignment_policy_mode: mode,
          tier_spillover_threshold_pct: spillover,
          fairness_window_days: windowDays,
          enable_morning_auto_rebalance: autoRebal,
        });
        if (matrixState.data && matrixState.data.policy) {
          matrixState.data.policy.assignment_policy_mode = mode;
          matrixState.data.policy.tier_spillover_threshold_pct = spillover;
          matrixState.data.policy.fairness_window_days = windowDays;
          matrixState.data.policy.enable_morning_auto_rebalance = autoRebal;
        }
        showToast(`Assignment policy saved (${mode}).`);
        onDashboardRefreshNeeded();
      } catch (err) {
        showError(err.message || "Failed to save assignment policy.");
      } finally {
        if (btn) btn.disabled = false;
      }
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      if (matrixState.activePopover) {
        matrixState.activePopover = null;
        renderMatrixModalBody();
        return;
      }
      closeAllModals();
    }
  });

  return {
    openMatrixModal,
    openRebalanceModal,
    closeAllModals,
  };
}
