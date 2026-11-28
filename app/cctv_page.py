"""CCTV 웹 뷰어 화면(로그인 창 + 반응형 뷰어).

외부 CSS·JS 없이 한 파일로 끝나게 해서 폰 브라우저에서도 가볍게 뜨도록 했다.
"""
from __future__ import annotations

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>초경량 CCTV · 로그인</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #0f1115; color: #e8eaed;
    font-family: -apple-system, BlinkMacSystemFont, "Malgun Gothic", "Apple SD Gothic Neo", sans-serif;
    padding: 24px;
  }
  .card {
    width: 100%; max-width: 360px; background: #171a21; border: 1px solid #262b36;
    border-radius: 16px; padding: 26px 22px; box-shadow: 0 10px 40px rgba(0,0,0,.45);
  }
  h1 { font-size: 19px; margin: 0 0 6px; letter-spacing: -.3px; }
  p.sub { margin: 0 0 20px; font-size: 13px; color: #9aa3b2; line-height: 1.5; }
  label { display: block; font-size: 12px; color: #9aa3b2; margin: 14px 0 6px; }
  input {
    width: 100%; padding: 13px 14px; font-size: 16px; border-radius: 10px;
    border: 1px solid #2d3340; background: #0f131a; color: #e8eaed; outline: none;
  }
  input:focus { border-color: #4c8dff; }
  button {
    width: 100%; margin-top: 20px; padding: 14px; font-size: 16px; font-weight: 700;
    border: 0; border-radius: 10px; background: #3b82f6; color: #fff; cursor: pointer;
  }
  button:active { background: #2f6fd8; }
  .err {
    margin-top: 16px; padding: 11px 13px; border-radius: 10px; font-size: 13px; line-height: 1.45;
    background: #3a1620; border: 1px solid #6b2130; color: #ffb4c0;
  }
  .lock { font-size: 22px; margin-bottom: 10px; }
</style>
</head>
<body>
  <form class="card" method="post" action="/cctv/login">
    <div class="lock">🔒</div>
    <h1>초경량 CCTV</h1>
    <p class="sub">이 화면은 비밀번호를 아는 사람만 볼 수 있습니다.</p>
    <label for="u">아이디</label>
    <input id="u" name="username" type="text" value="admin" autocomplete="username" autocapitalize="off">
    <label for="p">비밀번호</label>
    <input id="p" name="password" type="password" autocomplete="current-password" autofocus required>
    <button type="submit">들어가기</button>
    __ERROR__
  </form>
</body>
</html>
"""


VIEWER_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#0f1115">
<title>초경량 CCTV</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; }
  body {
    margin: 0; background: #0b0d11; color: #e8eaed;
    font-family: -apple-system, BlinkMacSystemFont, "Malgun Gothic", "Apple SD Gothic Neo", sans-serif;
    display: flex; flex-direction: column;
  }
  header {
    display: flex; align-items: center; gap: 10px; padding: 12px 14px;
    background: #12151c; border-bottom: 1px solid #212734; flex-wrap: wrap;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 700; letter-spacing: -.3px; white-space: nowrap; }
  .pill {
    display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #b8c0cd;
    background: #1a1f29; border: 1px solid #28303d; border-radius: 999px; padding: 5px 10px;
    white-space: nowrap;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #6b7280; }
  .dot.on { background: #22c55e; box-shadow: 0 0 0 3px rgba(34,197,94,.16); }
  .dot.off { background: #ef4444; box-shadow: 0 0 0 3px rgba(239,68,68,.14); }
  .spacer { flex: 1; }
  .linkbtn {
    background: none; border: 1px solid #2b3341; color: #b8c0cd; border-radius: 8px;
    padding: 7px 11px; font-size: 12px; cursor: pointer;
  }
  main { flex: 1; display: flex; flex-direction: column; gap: 10px; padding: 10px 12px 18px; }
  .stage {
    position: relative; width: 100%; background: #000; border-radius: 14px; overflow: hidden;
    border: 1px solid #1c2230; min-height: 220px;
    display: flex; align-items: center; justify-content: center;
  }
  .stage img { width: 100%; height: auto; display: block; }
  .stage .hint {
    position: absolute; inset: 0; display: flex; flex-direction: column; gap: 8px;
    align-items: center; justify-content: center; text-align: center; padding: 20px;
    color: #8d97a7; font-size: 13px; line-height: 1.6; background: #0a0c10;
  }
  .stage .hint b { color: #cfd6e2; font-size: 14px; }
  @media (min-width: 900px) { main { max-width: 1100px; margin: 0 auto; width: 100%; } }
  .controls { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .controls button {
    padding: 13px 8px; font-size: 13.5px; font-weight: 700; border-radius: 11px;
    border: 1px solid #2a3241; background: #161b24; color: #dfe5ee; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 46px;
  }
  .controls button:active { background: #202734; }
  .controls button.on { background: #1c3a2a; border-color: #2a6b45; color: #a7f3c6; }
  .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12.5px; color: #93a0b4; }
  .row .val { color: #dfe5ee; font-weight: 600; }
  .card {
    background: #10141b; border: 1px solid #1e2531; border-radius: 13px; padding: 13px 14px;
  }
  .card h2 { font-size: 13px; margin: 0 0 10px; color: #cfd6e2; }
  .card p { margin: 0 0 10px; font-size: 12.5px; color: #93a0b4; line-height: 1.55; }
  .card input {
    width: 100%; padding: 11px 12px; font-size: 15px; border-radius: 9px; margin-bottom: 8px;
    border: 1px solid #2a3241; background: #0b0f15; color: #e8eaed;
  }
  .card button.act {
    padding: 11px 14px; font-size: 13px; font-weight: 700; border-radius: 9px; border: 1px solid #2a3241;
    background: #1b2431; color: #dfe5ee; cursor: pointer;
  }
  .card button.act.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .msg { font-size: 12.5px; margin-top: 8px; min-height: 16px; color: #7dd3a0; }
  .msg.err { color: #ff9aa8; }
  details summary { cursor: pointer; font-size: 13px; color: #cfd6e2; font-weight: 600; }
  body.fullscreen .stage { position: fixed; inset: 0; z-index: 50; border-radius: 0; border: 0; }
  body.fullscreen .stage img { height: 100%; object-fit: contain; }
  body.fullscreen .stage .hint { background: #000; }
</style>
</head>
<body>
<header>
  <h1>📷 초경량 CCTV</h1>
  <span class="pill"><span class="dot" id="dot"></span><span id="conn">연결 확인 중</span></span>
  <span class="pill">🔋 <span id="battery">-</span></span>
  <span class="pill">🕒 <span id="lastframe">-</span></span>
  <span class="spacer"></span>
  <form method="post" action="/cctv/logout" style="margin:0">
    <button class="linkbtn" type="submit">로그아웃</button>
  </form>
</header>
<main>
  <div class="stage" id="stage">
    <img id="video" alt="카메라 영상" src="/api/cctv/stream">
    <div class="hint" id="hint">
      <b id="hintTitle">카메라 연결을 기다리는 중…</b>
      <span id="hintBody">폰에서 ‘초경량 CCTV’ 앱을 켜고 시작을 누르면 영상이 나타납니다.</span>
    </div>
  </div>

  <div class="controls">
    <button data-cmd="front" id="btnFront">🤳 전면</button>
    <button data-cmd="back" id="btnBack">📷 후면</button>
    <button data-cmd="flash_on" id="btnFlashOn">🔦 플래시 켜기</button>
    <button data-cmd="flash_off" id="btnFlashOff">🌙 플래시 끄기</button>
    <button id="btnSnap">💾 스냅샷</button>
    <button id="btnFull">⛶ 크게 보기</button>
  </div>
  <div class="controls">
    <button data-cmd="quality_low" id="btnQLow">화질 낮음</button>
    <button data-cmd="quality_mid" id="btnQMid">화질 중간</button>
    <button data-cmd="quality_high" id="btnQHigh">화질 높음</button>
  </div>

  <div class="row card">
    <span>기기 <span class="val" id="devname">-</span></span>
    <span>· 화질 <span class="val" id="quality">-</span></span>
    <span>· 카메라 <span class="val" id="camstat">-</span></span>
    <span>· 플래시 <span class="val" id="flash">-</span></span>
    <span>· 화면 <span class="val" id="screen">-</span></span>
    <span>· 앱 <span class="val" id="appv">-</span></span>
    <span>· <span id="streamState">-</span></span>
  </div>

  <div class="card">
    <details>
      <summary>⚙️ 설정 (비밀번호 변경 · 카메라 토큰)</summary>
      <p style="margin-top:10px">카메라 토큰은 폰 앱의 ‘카메라 토큰’ 칸에 넣는 값입니다. 바꾸면 폰 앱에도 새 값을 넣어야 합니다.</p>
      <div class="row" style="margin-bottom:10px">
        <span>현재 토큰</span>
        <span class="mono val" id="tokenHint">-</span>
      </div>
      <input id="newpw" type="password" placeholder="새 뷰어 비밀번호 (6자 이상)" autocomplete="new-password">
      <input id="newpw2" type="password" placeholder="새 비밀번호 한 번 더" autocomplete="new-password">
      <div class="row">
        <button class="act primary" id="btnSavePw">비밀번호 변경</button>
        <button class="act" id="btnNewToken">카메라 토큰 재발급</button>
      </div>
      <div class="msg" id="setMsg"></div>
    </details>
  </div>
</main>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var streamImg = $('video'), hint = $('hint');
  var qualityText = { low: '낮음', mid: '중간', high: '높음' };

  function restartStream() {
    streamImg.src = '/api/cctv/stream?t=' + Date.now();
  }

  streamImg.addEventListener('load', function () {});
  streamImg.addEventListener('error', function () { setTimeout(restartStream, 2500); });

  function post(path, data) {
    return fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data || {})
    }).then(function (r) {
      if (r.status === 401) { location.href = '/cctv'; throw new Error('로그인이 필요합니다.'); }
      return r.json().then(function (j) { if (!r.ok) throw new Error(j.detail || '요청 실패'); return j; });
    });
  }

  function fmtAgo(sec) {
    if (sec === null || sec === undefined) return '-';
    if (sec < 3) return '방금';
    if (sec < 60) return sec + '초 전';
    if (sec < 3600) return Math.floor(sec / 60) + '분 전';
    return Math.floor(sec / 3600) + '시간 전';
  }

  function refresh() {
    fetch('/api/cctv/status', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (r.status === 401) location.href = '/cctv'; return r.json(); })
      .then(function (s) {
        var online = !!s.camera_online;
        $('dot').className = 'dot ' + (online ? 'on' : 'off');
        $('conn').textContent = online ? '카메라 연결됨' : '카메라 오프라인';
        $('battery').textContent = (s.battery === null || s.battery === undefined)
          ? '-' : (s.battery + '%' + (s.charging ? ' ⚡' : ''));
        $('lastframe').textContent = '영상 ' + fmtAgo(s.last_frame_sec_ago);
        $('devname').textContent = s.device_name || '-';
        $('quality').textContent = qualityText[s.quality] || s.quality || '-';
        $('camstat').textContent = s.camera === 'front' ? '전면' : (s.camera === 'back' ? '후면' : '-');
        $('flash').textContent = s.flash ? '켜짐' : '꺼짐';
        $('screen').textContent = s.screen_on === false ? '꺼짐' : (s.screen_on ? '켜짐' : '-');
        $('appv').textContent = s.app_version || '-';
        $('tokenHint').textContent = s.camera_token_hint || '-';
        $('btnQLow').className = s.quality === 'low' ? 'on' : '';
        $('btnQMid').className = s.quality === 'mid' ? 'on' : '';
        $('btnQHigh').className = s.quality === 'high' ? 'on' : '';
        $('btnFront').className = s.camera === 'front' ? 'on' : '';
        $('btnBack').className = s.camera === 'back' ? 'on' : '';
        $('btnFlashOn').className = s.flash ? 'on' : '';
        $('btnFlashOff').className = s.flash ? '' : 'on';
        $('streamState').textContent = s.streaming_wanted ? '실시간 전송 중' : '대기(전송 안 함)';

        var fresh = s.last_frame_sec_ago !== null && s.last_frame_sec_ago <= 8;
        if (fresh) {
          hint.style.display = 'none';
        } else {
          hint.style.display = 'flex';
          if (online) {
            $('hintTitle').textContent = '영상을 기다리는 중…';
            $('hintBody').textContent = '카메라는 연결돼 있는데 아직 새 영상이 없습니다. 잠시만 기다려 주세요.';
          } else {
            $('hintTitle').textContent = '카메라가 꺼져 있습니다';
            $('hintBody').textContent = '폰에서 초경량 CCTV 앱을 켜고 [촬영 시작]을 눌러 주세요.';
          }
        }
      })
      .catch(function () {});
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-cmd]'), function (btn) {
    btn.addEventListener('click', function () {
      var cmd = btn.getAttribute('data-cmd');
      post('/api/cctv/command', { command: cmd }).catch(function (e) { alert(e.message); });
      setTimeout(refresh, 600);
    });
  });

  $('btnSnap').addEventListener('click', function () {
    fetch('/api/cctv/frame', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('아직 저장할 영상이 없습니다.');
        return r.blob();
      })
      .then(function (blob) {
        var d = new Date();
        var p = function (n) { return (n < 10 ? '0' : '') + n; };
        var name = 'cctv-' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-'
          + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '.jpg';
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      })
      .catch(function (e) { alert(e.message); });
  });

  $('btnFull').addEventListener('click', function () {
    document.body.classList.toggle('fullscreen');
    $('btnFull').textContent = document.body.classList.contains('fullscreen') ? '✕ 닫기' : '⛶ 크게 보기';
  });

  $('stage').addEventListener('click', function () {
    if (document.body.classList.contains('fullscreen')) {
      document.body.classList.remove('fullscreen');
      $('btnFull').textContent = '⛶ 크게 보기';
    }
  });

  function setMsg(text, isErr) {
    var m = $('setMsg');
    m.textContent = text;
    m.className = 'msg' + (isErr ? ' err' : '');
  }

  $('btnSavePw').addEventListener('click', function () {
    var a = $('newpw').value, b = $('newpw2').value;
    if (a.length < 6) { setMsg('비밀번호는 6자 이상이어야 합니다.', true); return; }
    if (a !== b) { setMsg('두 비밀번호가 다릅니다.', true); return; }
    post('/api/cctv/settings', { new_password: a })
      .then(function () {
        setMsg('비밀번호를 바꿨습니다. 다음 로그인부터 새 비밀번호를 쓰세요.');
        $('newpw').value = '';
        $('newpw2').value = '';
      })
      .catch(function (e) { setMsg(e.message, true); });
  });

  $('btnNewToken').addEventListener('click', function () {
    if (!confirm('카메라 토큰을 새로 만들까요? 폰 앱에도 새 토큰을 넣어야 영상이 올라옵니다.')) return;
    post('/api/cctv/settings', { regenerate_camera_token: true })
      .then(function (j) {
        setMsg('새 카메라 토큰: ' + j.camera_token + '  (폰 앱에 입력하세요)');
        refresh();
      })
      .catch(function (e) { setMsg(e.message, true); });
  });

  refresh();
  setInterval(refresh, 3000);
})();
</script>
</body>
</html>
"""


def render_login(error: str = "") -> str:
    block = '<div class="err">' + error + '</div>' if error else ""
    return LOGIN_HTML.replace("__ERROR__", block)


def render_viewer() -> str:
    return VIEWER_HTML
