// Daily Papers — topic filter, search, language memory
(function () {
  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  document.querySelectorAll('.lang a[data-lang]').forEach(function (a) {
    a.addEventListener('click', function () { store('dp-lang', a.dataset.lang); });
  });

  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  if (!cards.length) return;
  var filters = document.querySelectorAll('.filter');
  var search = document.querySelector('.search');
  var topic = '';

  var sections = document.querySelectorAll('.topic-sec');
  var otherItems = document.querySelectorAll('.others li[data-topics]');

  function hasTopic(el) {
    return !topic || (el.dataset.topics || '').split('|').indexOf(topic) >= 0;
  }
  function apply() {
    var q = (search && search.value || '').trim().toLowerCase();
    cards.forEach(function (c) {
      var okText = !q || (c.dataset.search || '').indexOf(q) >= 0;
      c.hidden = !(hasTopic(c) && okText);
    });
    sections.forEach(function (s) {
      s.hidden = !s.querySelector('.card:not([hidden])');
    });
    otherItems.forEach(function (li) { li.hidden = !hasTopic(li); });
  }
  filters.forEach(function (b) {
    b.addEventListener('click', function () {
      filters.forEach(function (x) { x.classList.remove('on'); });
      b.classList.add('on');
      topic = b.dataset.topic || '';
      apply();
    });
  });
  if (search) search.addEventListener('input', apply);
})();

// ---------------------------------------------------------------- 👍 / 👎 feedback
// Votes go straight from this browser to a private GitHub repo through api.github.com (no other
// server is involved). The repo name comes from <meta name="dp-feedback"> inside the page (which
// the published site encrypts). The reader's fine-grained token (Contents read/write on that one
// repo) is kept only in this browser's localStorage, AES-GCM-encrypted with the site key when the
// page was unlocked with the password, and is only ever sent to api.github.com.
(function () {
  var meta = document.querySelector('meta[name="dp-feedback"]');
  var boxes = document.querySelectorAll('.vote[data-key]');
  if (!meta || !boxes.length || !window.crypto || !crypto.subtle) return;
  // Only on the password-protected published site: the site key is what encrypts the token here.
  // On the plain local site the buttons stay hidden, so a token is never stored in clear text.
  if (!siteKeyRawEarly()) { boxes.forEach(function (b) { b.style.display = 'none'; }); return; }
  function siteKeyRawEarly() {
    try {
      return [sessionStorage, localStorage].some(function (st) {
        return Object.keys(st).some(function (k) { return k.indexOf('rn-key-') === 0; });
      });
    } catch (e) { return false; }
  }
  var API = 'https://api.github.com/repos/' + meta.content + '/contents/votes';
  var TK = 'dp-fb-token';
  var votes = {};

  function b64(buf) { var s = ''; new Uint8Array(buf).forEach(function (x) { s += String.fromCharCode(x); }); return btoa(s); }
  function unb64(s) { return Uint8Array.from(atob(s), function (c) { return c.charCodeAt(0); }); }
  function siteKeyRaw() {
    try {
      var stores = [sessionStorage, localStorage];
      for (var i = 0; i < stores.length; i++) {
        var ks = Object.keys(stores[i]);
        for (var j = 0; j < ks.length; j++) if (ks[j].indexOf('rn-key-') === 0) return stores[i].getItem(ks[j]);
      }
    } catch (e) {}
    return null;
  }
  function aesKey() {
    var raw = siteKeyRaw();
    return raw ? crypto.subtle.importKey('raw', unb64(raw), 'AES-GCM', false, ['encrypt', 'decrypt'])
               : Promise.resolve(null);
  }
  async function saveToken(tok) {
    var key = await aesKey();
    if (!key) return false;  // never store the token unencrypted
    var iv = crypto.getRandomValues(new Uint8Array(12));
    var ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv: iv }, key, new TextEncoder().encode(tok));
    try { localStorage.setItem(TK, JSON.stringify({ v: 1, iv: b64(iv), ct: b64(ct) })); } catch (e) { return false; }
    return true;
  }
  async function loadToken() {
    var rec;
    try { rec = JSON.parse(localStorage.getItem(TK) || 'null'); } catch (e) { rec = null; }
    if (!rec || !rec.ct) return null;
    var key = await aesKey();
    if (!key) return null;
    try {
      var pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: unb64(rec.iv) }, key, unb64(rec.ct));
      return new TextDecoder().decode(pt);
    } catch (e) { return null; }  // site password changed: ask again
  }
  function forgetToken() { try { localStorage.removeItem(TK); } catch (e) {} }

  function askToken() {
    return new Promise(function (resolve) {
      var wrap = document.createElement('div');
      wrap.className = 'fb-modal';
      wrap.innerHTML =
        '<form class="fb-box"><h3>피드백 토큰</h3>' +
        '<p>👍/👎를 저장하려면 피드백 repo 전용 GitHub fine-grained 토큰이 필요해요. ' +
        '이 브라우저에만 암호화되어 저장되고 api.github.com 외에는 보내지 않아요.</p>' +
        '<input type="password" autocomplete="off" spellcheck="false" placeholder="fine-grained 토큰 붙여넣기" required>' +
        '<p class="fb-err" hidden>토큰을 확인할 수 없어요. repo 접근 권한을 확인해 주세요.</p>' +
        '<div class="fb-row"><button type="button" class="fb-cancel">취소</button>' +
        '<button type="submit">저장</button></div></form>';
      document.body.appendChild(wrap);
      var input = wrap.querySelector('input'), err = wrap.querySelector('.fb-err');
      input.focus();
      wrap.querySelector('.fb-cancel').onclick = function () { wrap.remove(); resolve(null); };
      wrap.querySelector('form').onsubmit = async function (ev) {
        ev.preventDefault();
        var tok = input.value.trim();
        var r = await fetch('https://api.github.com/repos/' + meta.content,
                            { headers: { Authorization: 'Bearer ' + tok, Accept: 'application/vnd.github+json' } });
        if (!r.ok) { err.hidden = false; return; }
        input.value = '';
        await saveToken(tok);
        wrap.remove();
        resolve(tok);
      };
    });
  }

  function gh(method, url, tok, body) {
    return fetch(url, { method: method, cache: 'no-store',
      headers: { Authorization: 'Bearer ' + tok, Accept: 'application/vnd.github+json' },
      body: body ? JSON.stringify(body) : undefined });
  }
  // remember the server's order so un-voting puts a card back where it was
  document.querySelectorAll('.topic-sec .cards').forEach(function (list) {
    Array.prototype.forEach.call(list.children, function (c, i) { c.setAttribute('data-order', i); });
  });
  function reorder() {  // within each topic: 👍 first, 👎 last, the rest in the server's order
    document.querySelectorAll('.topic-sec .cards').forEach(function (list) {
      var cards = Array.prototype.slice.call(list.children);
      var rank = function (c) {
        var box = c.querySelector('.vote[data-key]'), v = box && votes[box.getAttribute('data-key')];
        return v ? (v.vote === 'up' ? 0 : 2) : 1;
      };
      cards.sort(function (a, b) {
        return rank(a) - rank(b) || (+a.getAttribute('data-order')) - (+b.getAttribute('data-order'));
      }).forEach(function (c) { list.appendChild(c); });
    });
  }
  function paint() {
    boxes.forEach(function (box) {
      var v = votes[box.getAttribute('data-key')];
      box.querySelectorAll('.vote-btn').forEach(function (b) {
        b.classList.toggle('on', !!v && v.vote === b.getAttribute('data-vote'));
      });
    });
    reorder();
  }
  async function refresh(tok) {
    var r = await gh('GET', API + '?per_page=1000', tok);
    if (r.status === 401 || r.status === 403) { forgetToken(); return false; }
    votes = {};
    if (r.ok) {
      (await r.json()).forEach(function (f) {
        var m = /^(\d{4}-\d{2}-\d{2})__([A-Za-z0-9._-]+)\.(up|down)\.json$/.exec(f.name);
        if (m) votes[m[2]] = { vote: m[3], name: f.name, sha: f.sha };
      });
    }
    paint();
    return true;
  }
  async function vote(box, want) {
    var tok = await loadToken() || await askToken();
    if (!tok) return;
    if (!Object.keys(votes).length && !(await refresh(tok))) { tok = await askToken(); if (!tok) return; await refresh(tok); }
    var key = box.getAttribute('data-key'), date = box.getAttribute('data-date'), cur = votes[key];
    box.classList.add('busy');
    try {
      if (cur) {
        var d = await gh('DELETE', API + '/' + cur.name, tok, { message: 'unvote', sha: cur.sha });
        if (!d.ok && d.status !== 404) throw d.status;
        delete votes[key];
      }
      if (!cur || cur.vote !== want) {
        var name = date + '__' + key + '.' + want + '.json';
        var content = btoa(JSON.stringify({ id: key, date: date, vote: want, at: new Date().toISOString() }));
        var p = await gh('PUT', API + '/' + name, tok, { message: 'vote', content: content });
        if (!p.ok) throw p.status;
        votes[key] = { vote: want, name: name, sha: (await p.json()).content.sha };
      }
    } catch (status) {
      if (status === 401 || status === 403) forgetToken();
      alert('저장하지 못했어요 (' + status + '). 잠시 후 다시 시도해 주세요.');
      await refresh(tok);
    }
    box.classList.remove('busy');
    paint();
  }

  boxes.forEach(function (box) {
    box.querySelectorAll('.vote-btn').forEach(function (b) {
      b.addEventListener('click', function () { vote(box, b.getAttribute('data-vote')); });
    });
  });
  loadToken().then(function (tok) { if (tok) refresh(tok); });

  var foot = document.querySelector('.foot');
  if (foot) {
    var a = document.createElement('a');
    a.href = '#'; a.className = 'fb-forget'; a.textContent = '피드백 토큰 지우기';
    a.onclick = function (ev) { ev.preventDefault(); forgetToken(); votes = {}; paint(); alert('이 브라우저에서 토큰을 지웠어요.'); };
    foot.appendChild(document.createElement('br')); foot.appendChild(a);
  }
})();
