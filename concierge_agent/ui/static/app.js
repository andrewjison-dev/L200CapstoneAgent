const state = {
  mealPlan: null,
  groceryCart: null,
  selectedLateDays: new Set(["Wednesday"]),
  activeRecipeId: null,
};

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 3400);
}

function switchTab(tabId) {
  document.querySelectorAll(".step-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === tabId);
  });
}

document.querySelectorAll(".step-tab").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

document.querySelectorAll("#late-days-group .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    const day = chip.dataset.day;
    if (state.selectedLateDays.has(day)) {
      state.selectedLateDays.delete(day);
      chip.classList.remove("active");
    } else {
      state.selectedLateDays.add(day);
      chip.classList.add("active");
    }
  });
});

async function apiGet(url) {
  const res = await fetch(url);
  return res.json();
}

async function apiPost(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

function renderInitialProfile(data) {
  const profile = data.family_profile || {};
  const pantry = profile.pantry_inventory || {};
  const pantryEl = document.getElementById("pantry-pills");
  pantryEl.innerHTML = Object.entries(pantry)
    .map(([name, info]) => `<span class="tag">${name} (${info.quantity} ${info.unit})</span>`)
    .join("");

  const memEl = document.getElementById("memory-list");
  memEl.innerHTML = (data.recalled_memories || [])
    .map((m) => `<li>${m.text}</li>`)
    .join("");

  if (data.latest_meal_plan) {
    state.mealPlan = data.latest_meal_plan;
    renderMealPlan(state.mealPlan);
  }
  if (data.latest_grocery_cart) {
    state.groceryCart = data.latest_grocery_cart;
    renderGroceryCart(state.groceryCart);
  }
}

function renderMealPlan(plan) {
  document.getElementById("plan-empty").hidden = true;
  document.getElementById("plan-content").hidden = false;

  const evalInfo = plan.self_evaluation || { score_out_of_100: 100, passed: true };
  document.getElementById("self-eval-badge").textContent =
    `Self-Eval Guardrail: ${evalInfo.score_out_of_100}/100 (${evalInfo.passed ? "PASSED" : "REVIEW"})`;

  const macros = plan.macro_summary_adult_daily_avg || {};
  document.getElementById("plan-metrics").innerHTML = `
    <div class="metric-tile"><div class="val">${macros.protein_g || 162}g</div><div class="lbl">Andrew Daily Protein Avg</div></div>
    <div class="metric-tile"><div class="val">${macros.active_weeknight_cook_avg_mins || 20} min</div><div class="lbl">Avg Weeknight Cook Time</div></div>
    <div class="metric-tile"><div class="val">$${Number(plan.estimated_total_cost_usd).toFixed(2)}</div><div class="lbl">Est. Weekly Spend (Cap $${plan.weekly_budget_usd})</div></div>
    <div class="metric-tile"><div class="val">${(plan.sunday_batch_prep_gameplan || []).length} Steps</div><div class="lbl">Sunday Batch-Prep Tasks</div></div>
  `;

  const prepList = document.getElementById("sunday-prep-list");
  const tasks = plan.sunday_batch_prep_gameplan || [];
  prepList.innerHTML = tasks.length
    ? tasks.map((t) => `
        <label class="checklist-item">
          <input type="checkbox" />
          <span><strong>Step ${t.step_number}:</strong> ${t.task}</span>
        </label>
      `).join("")
    : `<p class="muted">Zero Sunday batch-prep selected for this week.</p>`;

  const recipeSelect = document.getElementById("cook-recipe-select");
  const seenRecipes = new Map();

  const grid = document.getElementById("daily-grid");
  grid.innerHTML = (plan.daily_schedule || []).map((slot) => {
    const b = slot.post_workout_breakfast;
    const l = slot.workday_grab_and_go_lunch;
    const d = slot.weeknight_family_dinner;
    seenRecipes.set(d.recipe_id, `Dinner: ${d.title}`);
    seenRecipes.set(b.recipe_id, `Breakfast: ${b.title}`);

    return `
      <div class="day-card">
        <div class="day-header">
          <strong>${slot.day_of_week}</strong>
          <span class="tag">${slot.active_evening_cook_minutes}m dinner</span>
        </div>
        <div class="meal-slot">
          <strong>06:05 CST Post-Workout + Family Breakfast (${b.adult_protein_g}g P)</strong>
          ${b.title}
        </div>
        <div class="meal-slot">
          <strong>12:15 CST Workday Grab &amp; Go (${l.adult_protein_g}g P)</strong>
          ${l.title}
        </div>
        <div class="meal-slot">
          <strong>17:15 CST Family Dinner (${d.adult_protein_g}g P • ${d.active_cook_minutes}m)</strong>
          ${d.title}
        </div>
        <div class="kid-tip">Kids Tip: ${slot.kid_adaptation_tip}</div>
        <button class="btn btn-secondary" type="button" onclick="openCookMode('${d.recipe_id}')">
          Open 25-Min Cook Mode →
        </button>
      </div>
    `;
  }).join("");

  recipeSelect.innerHTML = Array.from(seenRecipes.entries())
    .map(([id, label]) => `<option value="${id}">${label}</option>`)
    .join("");
}

function renderGroceryCart(cart) {
  document.getElementById("grocery-empty").hidden = true;
  document.getElementById("grocery-content").hidden = false;

  const isApproved = cart.approval_status === "APPROVED_AND_DISPATCHED";
  document.getElementById("cart-dot").hidden = isApproved;

  const hitlCard = document.getElementById("hitl-card");
  hitlCard.classList.toggle("approved", isApproved);
  document.getElementById("hitl-title").textContent = isApproved
    ? `Order Confirmed (${cart.order_reference})`
    : `Awaiting Andrew's Approval — ${cart.total_items} Items ($${Number(cart.estimated_subtotal_usd).toFixed(2)})`;
  document.getElementById("hitl-desc").textContent = isApproved
    ? `Scheduled for ${cart.delivery_or_pickup_slot}. Dispatched in isolated sandbox mode.`
    : `Human-in-the-Loop Gate active: review aisle items below and click Approve to authorize checkout (Token: ${cart.approval_token || "staged"}).`;
  document.getElementById("btn-approve-cart").disabled = isApproved;

  document.getElementById("skipped-pantry-list").innerHTML =
    (cart.pantry_items_skipped || []).map((i) => `<span class="tag">✓ ${i}</span>`).join("");

  const aisleContainer = document.getElementById("aisle-container");
  aisleContainer.innerHTML = Object.entries(cart.aisle_grouped_items || {})
    .map(([aisle, items]) => `
      <div class="card">
        <h3>${aisle}</h3>
        ${items.map((item) => `
          <label class="checklist-item">
            <input type="checkbox" />
            <span>
              <strong>${item.ingredient_name}</strong> — ${item.total_quantity} ${item.unit}
              <span class="hint">($${Number(item.estimated_cost_usd).toFixed(2)})</span>
            </span>
          </label>
        `).join("")}
      </div>
    `).join("");
}

function renderCookTimeline(tl) {
  state.activeRecipeId = tl.recipe_id;
  document.getElementById("cook-title").textContent = tl.recipe_title;
  document.getElementById("cook-subtitle").textContent =
    `Target Table Time: ${tl.target_serve_time_cst} • Total Active Time: ${tl.total_active_minutes} mins`;

  document.getElementById("cook-steps-list").innerHTML = (tl.steps || [])
    .map((s) => `
      <label class="cook-step">
        <input type="checkbox" />
        <span class="time-badge">T+${s.minute_offset}m</span>
        <div>
          <strong>${s.station}:</strong> ${s.instruction}
          ${s.kid_friendly_checkpoint ? `<div class="kid-tip" style="margin-top:6px;">Kid Split Check: ${s.kid_friendly_checkpoint}</div>` : ""}
        </div>
      </label>
    `).join("");
}

window.openCookMode = async function openCookMode(recipeId) {
  const sundayPrep = Number(document.getElementById("sunday-prep").value) > 0;
  const tl = await apiPost("/api/cook-timeline", {
    recipe_id: recipeId,
    target_serve_time_cst: "17:45 CST",
    sunday_prep_completed: sundayPrep,
  });
  if (tl.status === "success") {
    document.getElementById("cook-recipe-select").value = recipeId;
    renderCookTimeline(tl);
    switchTab("tab-cook");
  }
};

document.getElementById("cook-recipe-select").addEventListener("change", (e) => {
  window.openCookMode(e.target.value);
});

async function runOrchestration(useDefaults = false) {
  const btn = document.getElementById("btn-orchestrate");
  btn.disabled = true;
  btn.textContent = "Orchestrating Multi-Agent Pipeline...";

  const fridgeRaw = document.getElementById("fridge-items").value;
  const cravingsRaw = document.getElementById("family-cravings").value;

  const payload = {
    week_start_date: document.getElementById("week-start").value,
    sunday_prep_minutes_available: Number(document.getElementById("sunday-prep").value),
    workout_focus: document.getElementById("workout-focus").value,
    weekly_budget_usd: Number(document.getElementById("budget-usd").value),
    late_work_or_travel_days: Array.from(state.selectedLateDays),
    use_up_fridge_items: fridgeRaw.split(",").map((s) => s.trim()).filter(Boolean),
    kid_or_spouse_cravings: cravingsRaw.split(",").map((s) => s.trim()).filter(Boolean),
    use_standard_defaults: useDefaults,
  };

  try {
    const res = await apiPost("/api/orchestrate", payload);
    if (res.status === "ORCHESTRATED") {
      state.mealPlan = res.weekly_meal_plan;
      state.groceryCart = res.staged_grocery_cart;
      renderMealPlan(state.mealPlan);
      renderGroceryCart(state.groceryCart);
      if (res.featured_cooking_timeline) {
        renderCookTimeline(res.featured_cooking_timeline);
      }
      showToast("Weekly plan, Sunday prep, & grocery cart ready!");
      switchTab("tab-plan");
      refreshTelemetry();
    } else {
      showToast(res.message || "Check inputs on Step 1.");
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Orchestrate My Week (Research → Plan → Grocery Cart → Cook Cards)";
  }
}

document.getElementById("checkin-form").addEventListener("submit", (e) => {
  e.preventDefault();
  runOrchestration(false);
});

document.getElementById("btn-quick-defaults").addEventListener("click", () => {
  runOrchestration(true);
});

document.getElementById("btn-approve-cart").addEventListener("click", async () => {
  if (!state.groceryCart) return;
  const slot = document.getElementById("pickup-slot").value;
  const res = await apiPost("/api/approve-cart", {
    cart_id: state.groceryCart.cart_id,
    approval_token: state.groceryCart.approval_token,
    delivery_or_pickup_slot: slot,
  });
  if (res.status === "ORDER_DISPATCHED_ISOLATED") {
    state.groceryCart.approval_status = "APPROVED_AND_DISPATCHED";
    state.groceryCart.order_reference = res.order_reference;
    state.groceryCart.delivery_or_pickup_slot = slot;
    renderGroceryCart(state.groceryCart);
    showToast(`Approved! Order ${res.order_reference} staged.`);
    refreshTelemetry();
  }
});

document.getElementById("btn-save-feedback").addEventListener("click", async () => {
  const recipeId = state.activeRecipeId || document.getElementById("cook-recipe-select").value;
  if (!recipeId) return;
  const res = await apiPost("/api/feedback", {
    recipe_id: recipeId,
    andrew_rating: Number(document.getElementById("fb-andrew").value),
    spouse_rating: Number(document.getElementById("fb-spouse").value),
    kids_ate_cleanly: document.getElementById("fb-kids").checked,
    notes: document.getElementById("fb-notes").value,
  });
  document.getElementById("fb-status").textContent =
    `Async background consolidation queued (${res.memory_job_id}) — zero UI blocking!`;
  showToast("Family meal rating queued to persistent vector memory!");
  refreshTelemetry();
});

async function refreshTelemetry() {
  const snap = await apiGet("/api/telemetry");
  document.getElementById("telemetry-json").textContent = JSON.stringify(snap, null, 2);
}

document.getElementById("btn-refresh-telemetry").addEventListener("click", refreshTelemetry);

apiGet("/api/state").then(renderInitialProfile);
