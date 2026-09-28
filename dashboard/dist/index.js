/**
 * hermes-a2a-bridge — Dashboard panel: A2A live switches.
 *
 * Three collector switches (collector.enabled / events / code_blocks) rendered
 * as one card at the top of the "Plugins" page (slot "plugins:top") and also
 * registered for the hidden tab route /hermes-a2a-bridge. Reads current values
 * from GET /api/plugins/hermes-a2a-bridge/collector; each toggle POSTs its own
 * key back. Changes take effect immediately (the gateway hot-reads
 * ctx.get_config on every hook / stream task) — no gateway restart needed.
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
  var KEYS = ["enabled", "events", "code_blocks"];

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
      codeBlocksLabel: "代码框渲染",
      codeBlocksDesc: "collector.code_blocks：开 → 代码框渲染；关 → 纯文本行。默认开。",
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
      codeBlocksLabel: "Code-block rendering",
      codeBlocksDesc: "collector.code_blocks: on → render as code blocks; off → plain-text lines. Default on.",
      loadError: "Failed to load switch state: ",
      saveError: "Failed to save: ",
    },
  };

  var SWITCH_META = [
    { key: "enabled", label: "enabledLabel", desc: "enabledDesc" },
    { key: "events", label: "eventsLabel", desc: "eventsDesc" },
    { key: "code_blocks", label: "codeBlocksLabel", desc: "codeBlocksDesc" },
  ];

  function errText(err) {
    if (err && err.message) return err.message;
    if (err && typeof err === "object") {
      try { return JSON.stringify(err); } catch (e) { /* ignore */ }
    }
    return String(err);
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
        var next = {};
        KEYS.forEach(function (k) {
          var item = res && res[k];
          next[k] = !!(item && item.value);
        });
        setValues(next);
      }).catch(function (err) {
        if (cancelled) return;
        setError(t.loadError + errText(err));
      });
      return function () { cancelled = true; };
    }, []);

    function onToggle(key) {
      return function (checked) {
        if (saving) return;
        setSaving(true);
        setError(null);
        setSaved(false);
        var body = {};
        body[key] = !!checked;
        SDK.fetchJSON(API, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }).then(function () {
          var next = {};
          KEYS.forEach(function (k) {
            next[k] = values ? !!values[k] : false;
          });
          next[key] = !!checked;
          setValues(next);
          setSaving(false);
          setSaved(true);
          showToast(t.saved, "success");
        }).catch(function (err) {
          setSaving(false);
          setSaved(false);
          setError(t.saveError + errText(err));
          showToast(t.saveError + errText(err), "error");
        });
      };
    }

    var rows = SWITCH_META.map(function (meta) {
      var checked = values ? !!values[meta.key] : false;
      return h("div", {
        key: meta.key,
        className: "h2ab-switch-row",
        // Stable hooks for isolated end-to-end verification (CDP) and for
        // users of assistive tech: the row names the switch and its state.
        "data-testid": "a2a-bridge-switch-" + meta.key,
        "data-key": meta.key,
        "data-checked": checked ? "true" : "false",
      }, h("div", { className: "h2ab-switch-info" },
        h("div", { className: "h2ab-switch-label" }, t[meta.label]),
        h("div", { className: "h2ab-switch-desc" }, t[meta.desc])),
        h(Checkbox, {
          checked: checked,
          disabled: !values || saving,
          onCheckedChange: onToggle(meta.key),
        }));
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
