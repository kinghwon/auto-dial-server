"""폰 브라우저용 APK 다운로드 페이지(/apps).

서버 `/data/apks/{app_id}/latest.json` 에 기록된 앱별 최신 APK 를 한 화면에 카드로 보여 준다.
외부 CDN·폰트에 의존하지 않는 한 파일짜리 HTML 이라 폰 데이터 상태에서도 바로 열린다.
"""
from __future__ import annotations

import html
from typing import Iterable, Mapping


def format_size(size_bytes: int) -> str:
    """바이트 크기를 사람이 읽기 쉬운 문자열로(예: 10.5MB)."""
    try:
        size = int(size_bytes or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return "-"
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _text(value: object, fallback: str = "-") -> str:
    text = str(value or "").strip()
    return html.escape(text) if text else fallback


def _format_updated(value: object) -> str:
    """2026-11-24T23:42:16+09:00 → 2026-11-24 23:42"""
    text = str(value or "").strip()
    if not text:
        return "-"
    try:
        return html.escape(text.replace("T", " ")[:16])
    except Exception:  # noqa: BLE001 - 표시용이라 실패해도 원문을 쓴다
        return html.escape(text)


def _app_card(app: Mapping[str, object]) -> str:
    name = _text(app.get("display_name") or app.get("app_id"), "이름 없음")
    app_id = _text(app.get("app_id"))
    description = _text(app.get("description"), "")
    version_name = _text(app.get("version_name"), "")
    version_code = app.get("version_code") or 0
    size_text = _text(app.get("size_text") or format_size(int(app.get("size_bytes") or 0)))
    updated = _format_updated(app.get("updated_at"))
    notes = _text(app.get("release_notes"), "")
    download_url = str(app.get("download_url") or "").strip()
    available = bool(app.get("available"))

    if available and download_url:
        action = (
            f'<a class="dl" href="{html.escape(download_url, quote=True)}" '
            f'download>⬇️ APK 다운로드</a>'
        )
    else:
        action = '<span class="dl disabled">파일 없음</span>'

    desc_html = f'<div class="desc">{description}</div>' if description else ""
    notes_html = f'<div class="notes"><span class="notes-label">업데이트 내용</span>{notes}</div>' if notes else ""
    version_html = f'<span class="ver">v{html.escape(version_name)}</span>' if version_name else ""
    code_html = f'<span class="code">code {int(version_code)}</span>' if version_code else ""

    return f"""
      <article class="card">
        <div class="card-head">
          <div class="icon">📱</div>
          <div class="names">
            <h2>{name}</h2>
            <div class="app-id">{app_id}</div>
          </div>
          <div class="ver-box">{version_html}{code_html}</div>
        </div>
        {desc_html}
        <div class="meta">
          <div><span>크기</span><b>{size_text}</b></div>
          <div><span>올린 날짜</span><b>{updated}</b></div>
        </div>
        {notes_html}
        {action}
      </article>"""


def render_apps_page(
    apps: Iterable[Mapping[str, object]],
    server_label: str = "자동 발신 서버",
    admin_url: str = "/",
) -> str:
    """앱 목록으로 모바일 친화적인 다운로드 페이지 HTML 을 만든다."""
    items = list(apps or [])
    cards = "\n".join(_app_card(app) for app in items)
    if not cards:
        cards = """
      <div class="empty">
        <div class="empty-icon">📦</div>
        <p>아직 서버에 올린 앱이 없습니다.</p>
        <p class="muted">앱 APK 를 서버의 <code>/api/apps/{app_id}/upload</code> 로 올리면 여기에 표시됩니다.</p>
      </div>"""

    count_text = f"{len(items)}개 앱" if items else "등록된 앱 없음"
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#16233d">
<title>앱 다운로드 · {html.escape(server_label)}</title>
<style>
  :root{{
    --bg:#f4f6fb; --card:#ffffff; --line:#e4e9f2; --text:#182033; --muted:#6d7b91;
    --primary:#2f6bed; --primary-dark:#1f52c4; --ok:#0f9d58; --warn:#c77700;
    --radius:16px; --shadow:0 2px 4px rgba(16,24,40,.05), 0 10px 28px rgba(16,24,40,.07);
  }}
  *{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
  html,body{{margin:0;padding:0}}
  body{{
    background:var(--bg); color:var(--text); font-size:15px; line-height:1.55;
    font-family:"Pretendard","Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased;
    padding:0 0 calc(28px + env(safe-area-inset-bottom));
  }}
  a{{color:var(--primary);text-decoration:none}}
  .top{{
    background:linear-gradient(135deg,#16233d,#2b4573); color:#fff;
    padding:calc(20px + env(safe-area-inset-top)) 18px 20px;
    border-radius:0 0 22px 22px; box-shadow:0 4px 18px rgba(12,22,42,.18);
  }}
  .top h1{{margin:0;font-size:21px;font-weight:800;letter-spacing:-.3px}}
  .top .sub{{margin-top:4px;font-size:12.5px;color:#b9c8e2}}
  .top .badge{{
    display:inline-flex;align-items:center;gap:6px;margin-top:12px;
    background:rgba(255,255,255,.13);padding:5px 11px;border-radius:999px;font-size:12px;color:#e3ecfb
  }}
  .wrap{{padding:16px 14px 0;max-width:640px;margin:0 auto}}
  .card{{
    background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    box-shadow:var(--shadow);padding:16px;margin-bottom:14px;
  }}
  .card-head{{display:flex;align-items:flex-start;gap:12px}}
  .icon{{
    width:46px;height:46px;flex:0 0 46px;border-radius:13px;background:#eaf1ff;
    display:grid;place-items:center;font-size:22px;
  }}
  .names{{flex:1;min-width:0}}
  .names h2{{margin:0;font-size:17px;font-weight:800;letter-spacing:-.2px}}
  .app-id{{font-size:11.5px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin-top:2px}}
  .ver-box{{display:flex;flex-direction:column;align-items:flex-end;gap:3px}}
  .ver{{background:#e7f7ee;color:#0b7a45;font-weight:800;font-size:12.5px;padding:3px 9px;border-radius:999px}}
  .code{{font-size:10.5px;color:var(--muted);font-variant-numeric:tabular-nums}}
  .desc{{margin-top:10px;font-size:13px;color:#455468}}
  .meta{{
    display:flex;gap:10px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line);
  }}
  .meta > div{{flex:1;min-width:0}}
  .meta span{{display:block;font-size:11px;color:var(--muted);margin-bottom:1px}}
  .meta b{{display:block;font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}}
  .notes{{
    margin-top:12px;background:#f8faff;border:1px solid #e8eefb;border-radius:12px;
    padding:10px 12px;font-size:12.5px;color:#3c4a60;white-space:pre-wrap;word-break:break-word;
  }}
  .notes-label{{display:block;font-size:11px;font-weight:800;color:#5a6b86;margin-bottom:3px}}
  .dl{{
    display:block;margin-top:14px;text-align:center;background:var(--primary);color:#fff;
    font-weight:800;font-size:15.5px;padding:14px 12px;border-radius:13px;
    box-shadow:0 6px 16px rgba(47,107,237,.28);transition:transform .08s ease, background .15s;
  }}
  .dl:active{{transform:scale(.985);background:var(--primary-dark)}}
  .dl.disabled{{background:#e6eaf2;color:#8a97ab;box-shadow:none}}
  .hint{{
    margin:4px 2px 0;font-size:12px;color:var(--muted);line-height:1.6;
  }}
  .hint b{{color:#3c4a60}}
  .actions{{display:flex;gap:10px;margin-top:16px}}
  .actions a, .actions button{{
    flex:1;text-align:center;font-family:inherit;font-size:13.5px;font-weight:700;cursor:pointer;
    padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:#fff;color:#3c4a60;
  }}
  .empty{{text-align:center;padding:38px 16px;color:#4a5a72}}
  .empty-icon{{font-size:38px;margin-bottom:8px}}
  .empty p{{margin:4px 0}}
  .muted{{color:var(--muted);font-size:12.5px}}
  code{{background:#eef2f9;padding:1px 5px;border-radius:5px;font-size:12px}}
  .foot{{margin:18px 2px 0;font-size:11.5px;color:var(--muted);text-align:center}}
</style>
</head>
<body>
  <header class="top">
    <h1>📱 앱 다운로드</h1>
    <div class="sub">{html.escape(server_label)}에 올린 최신 앱을 이 폰에서 바로 설치합니다</div>
    <div class="badge">최신 버전 · {html.escape(count_text)}</div>
  </header>

  <main class="wrap">
    {cards}

    <div class="hint">
      <b>설치 방법</b> — 내려받은 파일을 누르면 설치 화면이 열립니다.
      처음 설치할 때 “출처를 알 수 없는 앱” 설치를 허용해야 합니다.
      이미 설치한 앱은 같은 서명의 새 버전이면 그대로 덮어써지고, 저장된 설정·서버 주소는 유지됩니다.
    </div>

    <div class="actions">
      <button type="button" onclick="location.reload()">🔄 새로고침</button>
      <a href="/cctv">📷 CCTV 뷰어 열기</a>
      <a href="{html.escape(admin_url, quote=True)}">관리 대시보드</a>
    </div>
    <div class="foot">APK 는 서버에서 직접 전송됩니다 · 내려받은 파일의 SHA-256 은 각 앱의 업데이트 정보와 같습니다</div>
  </main>
</body>
</html>
"""
