from __future__ import annotations

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class PageContext:
    """Minimal shared page metadata used by the HTML wrapper."""

    page_title: str
    state: str
    message: str = ""
    subtitle: str = ""


def _base_page_css() -> str:
    """Return shared CSS used by all BSM web pages."""

    return """
    :root {
      --bg: #f2f4f7;
      --panel: #ffffff;
      --text: #1f2937;
      --line: #c7d2de;
      --primary: #0b5ea8;
      --accent: #e7f1fb;
    }
    body {
      margin: 0;
      background: linear-gradient(180deg, #f7fafc 0%, var(--bg) 100%);
      color: var(--text);
      font-family: "Avenir Next", "Trebuchet MS", sans-serif;
    }
    .shell { max-width: 1180px; margin: 1.25rem auto; padding: 0 1rem; }
    .panel {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
      padding: 1rem;
      box-shadow: 0 6px 20px rgba(23, 43, 77, 0.08);
    }
    .title { margin: 0; color: #0b2d4b; letter-spacing: 0.02em; }
    .subtitle { margin: 0.25rem 0 0.8rem 0; color: #304a64; font-weight: 700; }
    .status { margin: 0 0 0.75rem 0; font-weight: 600; }
    .controls { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
    form { margin: 0; }
    button {
      border: 1px solid #2b6cb0;
      background: var(--primary);
      color: white;
      border-radius: 6px;
      padding: 0.55rem 0.9rem;
      font-size: 0.95rem;
      cursor: pointer;
    }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .section-title { margin: 0.9rem 0 0.4rem 0; font-size: 0.95rem; color: #304a64; font-weight: 700; }
    .scrollbox {
      border: 1px solid var(--line);
      background: #fbfdff;
      border-radius: 6px;
      height: 260px;
      overflow: auto;
      padding: 0.65rem;
      white-space: pre;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.84rem;
      line-height: 1.35;
    }
    .known-arduino-box { height: 320px; }
    .known-sort-control {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      margin: 0 0 0.45rem 0;
      font-family: "Avenir Next", "Trebuchet MS", sans-serif;
      font-size: 0.9rem;
      font-weight: 700;
      color: #304a64;
    }
    .known-sort-control select {
      padding: 0.35rem 0.45rem;
      border: 1px solid #9cb2c9;
      border-radius: 4px;
      background: #fff;
      color: #1f2937;
      font-size: 0.9rem;
    }
    """


def _render_shared_page(
    *,
    ctx: PageContext,
    body_html: str,
    web_app_header: str,
    active_network_profile: str,
    active_network_profile_source: str,
    extra_css: str = "",
    script_js: str = "",
) -> bytes:
    """Render a full HTML document from shared frame + per-page body/script."""

    msg_html = f"<p><strong>{html.escape(ctx.message)}</strong></p>" if ctx.message else ""
    subtitle_html = f'<div class="subtitle">{html.escape(ctx.subtitle)}</div>' if ctx.subtitle else ""
    script_block = f"\n<script>\n{script_js}\n</script>" if script_js else ""
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(ctx.page_title)}</title>
  <style>
{_base_page_css()}
{extra_css}
  </style>
</head>
<body>
  <div class="shell">
    <div class="panel">
      <h2 class="title">{web_app_header}</h2>
      {subtitle_html}
      <div class="status">Status: <strong>{html.escape(ctx.state)}</strong></div>
      <div class="status">Profile: <strong>{html.escape(active_network_profile)}</strong> ({html.escape(active_network_profile_source)})</div>
      {msg_html}
{body_html}
    </div>
  </div>{script_block}
</body>
</html>
"""
    return page.encode("utf-8")


def _render_section_title(title: str) -> str:
    """Render a standardized section header."""

    return f'<div class="section-title">{html.escape(title)}</div>'


def _render_scrollbox(box_id: str, initial_text: str, extra_classes: str = "") -> str:
    """Render a monospaced scrollable text panel."""

    classes = "scrollbox"
    if extra_classes:
        classes = f"{classes} {extra_classes.strip()}"
    return f'<div id="{html.escape(box_id)}" class="{html.escape(classes)}">{html.escape(initial_text)}</div>'


def _render_titled_scroll_panel(title: str, box_id: str, initial_text: str, extra_classes: str = "") -> str:
    """Render a section title followed by a scrollbox panel."""

    return f"{_render_section_title(title)}\n{_render_scrollbox(box_id, initial_text, extra_classes)}"


def _render_known_arduinos_selector(
    *,
    action: str,
    selected_uid: str,
    device_rows_html_block: str,
    sort_mode: str = "last_seen",
    button_label: str = "",
    button_class: str = "needs-device",
    show_button: bool = True,
) -> str:
    """Render the common 'Known Arduinos' selector block."""

    sort_options = [
        ("last_seen", "Last seen"),
        ("burrow_id", "Burrow ID"),
        ("short_uid", "Short UID"),
    ]
    sort_html = "\n".join(
        f'      <option value="{html.escape(value)}"{" selected" if sort_mode == value else ""}>{html.escape(label)}</option>'
        for value, label in sort_options
    )
    button_html = ""
    if show_button:
        button_html = (
            f"  <div style=\"margin-top:0.5rem;\">\n"
            f"    <button type=\"submit\" class=\"{html.escape(button_class)}\">{html.escape(button_label)}</button>\n"
            "  </div>\n"
        )
    return (
        f"{_render_section_title('Known Arduinos (select one)')}\n"
        f"<form method=\"get\" action=\"{html.escape(action)}\">\n"
        f"  <input type=\"hidden\" name=\"uid\" value=\"{html.escape(selected_uid)}\" class=\"selected-uid-field\" />\n"
        "  <div class=\"known-sort-control\">\n"
        "    <label for=\"known_sort\">Sort:</label>\n"
        f"    <select id=\"known_sort\" name=\"sort\" onchange=\"this.form.submit()\">\n{sort_html}\n    </select>\n"
        "  </div>\n"
        f"  <div class=\"scrollbox known-arduino-box\">{device_rows_html_block}</div>\n"
        f"{button_html}"
        "</form>"
    )


def _render_action_form(
    *,
    action: str,
    label: str,
    method: str = "post",
    hidden_fields: list[tuple[str, str]] | None = None,
    button_class: str = "",
    button_id: str = "",
    button_name: str = "",
    button_value: str = "",
    form_id: str = "",
    hidden_input_class: str = "",
    hidden_class_names: set[str] | None = None,
) -> str:
    """Render a reusable form+button control with optional hidden fields."""

    attrs = []
    if form_id:
        attrs.append(f'id="{html.escape(form_id)}"')
    attrs_txt = (" " + " ".join(attrs)) if attrs else ""
    lines = [f'<form method="{html.escape(method)}" action="{html.escape(action)}"{attrs_txt}>']
    for key, value in (hidden_fields or []):
        use_class = False
        if hidden_input_class:
            if hidden_class_names is None:
                use_class = True
            elif key in hidden_class_names:
                use_class = True
        class_attr = f' class="{html.escape(hidden_input_class)}"' if use_class else ""
        lines.append(
            f'  <input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}"{class_attr} />'
        )
    btn_attrs = []
    if button_class:
        btn_attrs.append(f'class="{html.escape(button_class)}"')
    if button_id:
        btn_attrs.append(f'id="{html.escape(button_id)}"')
    if button_name:
        btn_attrs.append(f'name="{html.escape(button_name)}"')
    if button_value:
        btn_attrs.append(f'value="{html.escape(button_value)}"')
    btn_attrs_txt = (" " + " ".join(btn_attrs)) if btn_attrs else ""
    lines.append(f'  <button type="submit"{btn_attrs_txt}>{html.escape(label)}</button>')
    lines.append("</form>")
    return "\n".join(lines)


def _render_controls_row(forms_html: list[str], extra_style: str = "") -> str:
    """Render a horizontal controls row made of pre-rendered form blocks."""

    style_attr = f' style="{html.escape(extra_style)}"' if extra_style else ""
    inner = "\n".join(forms_html)
    return f'<div class="controls"{style_attr}>\n{inner}\n</div>'
