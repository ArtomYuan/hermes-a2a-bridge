/**
 * hermes-a2a-bridge — Dashboard panel: A2A live switches.
 *
 * Collector settings rendered as one card at the top of the "Plugins" page
 * (slot "plugins:top") and also registered for the hidden tab route
 * /hermes-a2a-bridge. Two toggles (collector.enabled / collector.events) plus
 * one detail-level dropdown (collector.live_detail: follow-dsh / compact /
 * standard / detailed / verbose). Reads current values from
 * GET /api/plugins/hermes-a2a-bridge/collector; each control POSTs its own key
 * back. Changes take effect immediately (the gateway hot-reads ctx.get_config
 * on every hook / stream task) — no gateway restart needed.
 *
 * The legacy collector.content boolean was removed from this panel (it is
 * still honoured at the config layer for backward compatibility, mapped to
 * live_detail); live_detail is a string dropdown whose GET response also
 * carries the resolved "effective" level for the "follow dsh" mode.
 *
 * Plain IIFE classic script, no build step. All dependencies come from
 * window.__HERMES_PLUGIN_SDK__; bilingual zh/en copy is self-contained and
 * follows the host page language (SDK.useI18n().locale).
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  var h = SDK.React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var Card = SDK.components.Card;
  var CardHeader = SDK.components.CardHeader;
  var CardTitle = SDK.components.CardTitle;
  var CardContent = SDK.components.CardContent;
  var Badge = SDK.components.Badge;

  // Newer host dashboards expose a DS-styled Checkbox on the plugin SDK.
  // Fall back to a native <input type="checkbox"> shim so older hosts still
  // render (kanban-style shim; normalises Radix onCheckedChange(checked)).
  var Checkbox = SDK.components.Checkbox || function (props) {
    var p = props || {};
    return h("input", {
      type: "checkbox",
      checked: !!p.checked,
      disabled: !!p.disabled,
      className: p.className,
      onChange: function (e) {
        if (p.onCheckedChange) p.onCheckedChange(e.target.checked);
      },
    });
  };

  // useI18n / useToast may be missing on older hosts; shims keep the bundle alive.
  var useI18n = SDK.useI18n || function () { return { locale: "en" }; };
  var useToast = (SDK.hooks && SDK.hooks.useToast) || function () {
    return { showToast: function () {}, toast: null };
  };
  var Toast = SDK.components.Toast;

  var PLUGIN_NAME = "hermes-a2a-bridge";
  var API = "/api/plugins/hermes-a2a-bridge/collector";
  var KEYS = ["enabled", "events", "live_detail"];

  // live_detail 下拉选项（5 项，与后端 _LIVE_DETAIL_VALUES 一致）。
  var LIVE_DETAIL_OPTIONS = ["follow-dsh", "compact", "standard", "detailed", "verbose"];

  var T = {
    zh: {
      title: "A2A 直播开关",
      subtitle: "A2A live switches",
      hotBadge: "即时生效 · 无需重启网关",
      loading: "加载中…",
      saving: "保存中…",
      saved: "已保存 ✓",
      enabledLabel: "直播总开关",
      enabledDesc: "collector.enabled：开 → dsh 任务走单执行直播；关 → 仅注入 origin。默认关。",
      eventsLabel: "中间事件推送",
      eventsDesc: "collector.events：开 → 中间事件 + 最终结果都推；关 → 安静模式只推最终结果。默认开。",
      liveDetailLabel: "工作步骤展示",
      liveDetailDesc: "collector.live_detail：跟随 dsh / 简洁 / 标准 / 详细 / 完全展开，决定直播过程细节的多少。默认跟随 dsh。",
      effectiveHint: "实际生效：",
      liveDetailOptions: {
        "follow-dsh": "跟随 dsh",
        "compact": "简洁",
        "standard": "标准",
        "detailed": "详细",
        "verbose": "完全展开",
      },
      loadError: "加载开关状态失败：",
      saveError: "保存失败：",
    },
    en: {
      title: "A2A live switches",
      subtitle: "A2A 直播开关",
      hotBadge: "Instant · no gateway restart",
      loading: "Loading…",
      saving: "Saving…",
      saved: "Saved ✓",
      enabledLabel: "Live stream master switch",
      enabledDesc: "collector.enabled: on → single-execution live stream for dsh tasks; off → origin injection only. Default off.",
      eventsLabel: "Intermediate events",
      eventsDesc: "collector.events: on → push intermediate events + final result; off → quiet mode, final result only. Default on.",
      liveDetailLabel: "Work details",
      liveDetailDesc: "collector.live_detail: follow dsh / compact / standard / detailed / verbose — how much live process detail to show. Default follows dsh.",
      effectiveHint: "Effective: ",
      liveDetailOptions: {
        "follow-dsh": "Follow dsh",
        "compact": "Compact",
        "standard": "Standard",
        "detailed": "Detailed",
        "verbose": "Verbose",
      },
      loadError: "Failed to load switch state: ",
      saveError: "Failed to save: ",
    },
  };

  var SWITCH_META = [
    { key: "enabled", kind: "toggle", label: "enabledLabel", desc: "enabledDesc" },
    { key: "events", kind: "toggle", label: "eventsLabel", desc: "eventsDesc" },
    { key: "live_detail", kind: "select", label: "liveDetailLabel", desc: "liveDetailDesc" },
  ];

  function errText(err) {
    if (err && err.message) return err.message;
    if (err && typeof err === "object") {
      try { return JSON.stringify(err); } catch (e) { /* ignore */ }
    }
    return String(err);
  }

  function readState(res) {
    var ld = res && res.live_detail;
    return {
      enabled: !!(res && res.enabled && res.enabled.value),
      events: !!(res && res.events && res.events.value),
      live_detail: (ld && ld.value) || "follow-dsh",
      effective: (ld && ld.effective) || "",
    };
  }

  function Panel() {
    var i18n = useI18n();
    var locale = i18n && i18n.locale ? String(i18n.locale).toLowerCase() : "";
    var lang = locale.indexOf("zh") === 0 ? "zh" : "en";
    var t = T[lang] || T.en;

    var toastCtx = useToast();
    var showToast = toastCtx.showToast || function () {};
    var toast = toastCtx.toast;

    var valuesState = useState(null);
    var values = valuesState[0];
    var setValues = valuesState[1];
    var savingState = useState(false);
    var saving = savingState[0];
    var setSaving = savingState[1];
    var errorState = useState(null);
    var error = errorState[0];
    var setError = errorState[1];
    var savedState = useState(false);
    var saved = savedState[0];
    var setSaved = savedState[1];

    useEffect(function () {
      var cancelled = false;
      SDK.fetchJSON(API).then(function (res) {
        if (cancelled) return;
        setValues(readState(res));
      }).catch(function (err) {
        if (cancelled) return;
        setError(t.loadError + errText(err));
      });
      return function () { cancelled = true; };
    }, []);

    function onChange(key, value) {
      if (saving) return;
      setSaving(true);
      setError(null);
      setSaved(false);
      var body = {};
      body[key] = value;
      SDK.fetchJSON(API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function (res) {
        // 响应是写后从磁盘读回的完整状态，直接回填（含 live_detail.effective）。
        setValues(readState(res));
        setSaving(false);
        setSaved(true);
        showToast(t.saved, "success");
      }).catch(function (err) {
        setSaving(false);
        setSaved(false);
        setError(t.saveError + errText(err));
        showToast(t.saveError + errText(err), "error");
      });
    }

    var rows = SWITCH_META.map(function (meta) {
      var isSelect = meta.kind === "select";
      var checked = values ? !!values[meta.key] : false;
      var value = values ? values[meta.key] : (isSelect ? "follow-dsh" : false);
      var control;

      if (isSelect) {
        control = h("select", {
          className: "h2ab-switch-select",
          value: String(value || "follow-dsh"),
          disabled: !values || saving,
          "data-testid": "a2a-bridge-select-" + meta.key,
          style: { minWidth: "150px", fontSize: "13px", padding: "4px 6px", marginTop: "3px" },
          onChange: function (e) { onChange(meta.key, e.target.value); },
        }, LIVE_DETAIL_OPTIONS.map(function (opt) {
          return h("option", { key: opt, value: opt }, t.liveDetailOptions[opt] || opt);
        }));
      } else {
        control = h(Checkbox, {
          checked: checked,
          disabled: !values || saving,
          onCheckedChange: function (next) { onChange(meta.key, !!next); },
        });
      }

      var infoChildren = [
        h("div", { className: "h2ab-switch-label" }, t[meta.label]),
        h("div", { className: "h2ab-switch-desc" }, t[meta.desc]),
      ];
      if (isSelect && values && values.live_detail === "follow-dsh" && values.effective) {
        infoChildren.push(
          h("div", {
            className: "h2ab-switch-effective",
            style: { fontSize: "12px", opacity: 0.72, marginTop: "2px" },
          }, t.effectiveHint + (t.liveDetailOptions[values.effective] || values.effective))
        );
      }

      return h("div", {
        key: meta.key,
        className: "h2ab-switch-row",
        // Stable hooks for isolated end-to-end verification (CDP) and for
        // users of assistive tech: the row names the switch and its state.
        "data-testid": "a2a-bridge-switch-" + meta.key,
        "data-key": meta.key,
        "data-checked": isSelect ? null : (checked ? "true" : "false"),
        "data-value": isSelect ? String(value || "follow-dsh") : null,
      }, h("div", { className: "h2ab-switch-info" }, infoChildren), control);
    });

    return h(Card, { className: "h2ab-card" },
      h(CardHeader, { className: "h2ab-card-header" },
        h("div", null,
          h(CardTitle, null, t.title),
          h("p", { className: "h2ab-subtitle" }, t.subtitle)),
        h(Badge, { className: "h2ab-hot-badge" }, t.hotBadge)),
      h(CardContent, null,
        h("div", {
          "data-testid": "a2a-bridge-panel",
          "data-state": values === null ? "loading" : "ready",
        },
          error ? h("div", { className: "h2ab-error" }, error) : null,
          values === null ? h("div", { className: "h2ab-loading" },
            t.loading + (saving ? " " + t.saving : "")) : rows,
          saving ? h("div", { className: "h2ab-loading" }, t.saving) : null,
          saved ? h("div", { className: "h2ab-saved" }, t.saved) : null)),
      Toast ? h(Toast, { toast: toast }) : null);
  }

  var P = window.__HERMES_PLUGINS__;
  if (P && typeof P.registerSlot === "function") {
    // registerSlot(plugin, slot, component) — plugin first (implementation order).
    P.registerSlot(PLUGIN_NAME, "plugins:top", Panel);
  }
  if (P && typeof P.register === "function") {
    // Hidden tab route fallback (/hermes-a2a-bridge); also marks the bundle
    // as registered so the host does not flag NO_REGISTER.
    P.register(PLUGIN_NAME, Panel);
  }
})();
