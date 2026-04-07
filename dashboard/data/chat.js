/* chat.js — Atlas Dashboard WebSocket chat client
   Depends on: chat.css, Basic Auth already active on the page.
   Init: Chat.init() called at end of index.html after page load.
*/
'use strict';

var Chat = (function () {

  // ── State ────────────────────────────────────────────────────────────────
  var ws = null;
  var currentSessionId = null;
  var isGenerating = false;
  var sessionCost = 0;
  var reconnectAttempts = 0;
  var MAX_RECONNECT = 10;
  var currentAssistantEl = null;
  var messagesEl = null;

  // ── Auth token (Basic Auth → short-lived WS token) ───────────────────────
  function getAuthToken() {
    var cached = sessionStorage.getItem('atlas-ws-token');
    if (cached) return cached;

    try {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', '/api/chat/token', false); // synchronous
      xhr.send();
      if (xhr.status === 200) {
        var resp = JSON.parse(xhr.responseText);
        var token = resp.token || '';
        if (token) {
          sessionStorage.setItem('atlas-ws-token', token);
          return token;
        }
      }
    } catch (e) {
      console.warn('[chat] token fetch failed:', e);
    }
    return '';
  }

  // ── WebSocket lifecycle ──────────────────────────────────────────────────
  function connect(sessionId) {
    if (ws && ws.readyState <= 1) return; // already open or connecting

    var token = getAuthToken();
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/chat?token=' + encodeURIComponent(token);

    try {
      ws = new WebSocket(url);
    } catch (e) {
      console.error('[chat] WebSocket constructor failed:', e);
      updateStatus('error');
      return;
    }

    ws.onopen = function () {
      reconnectAttempts = 0;
      updateStatus('connected');
      // Request history for the current session
      if (currentSessionId) {
        ws.send(JSON.stringify({
          type: 'history',
          session_id: currentSessionId,
          limit: 50,
        }));
      }
    };

    ws.onmessage = function (e) {
      try {
        var msg = JSON.parse(e.data);
        handleMessage(msg);
      } catch (err) {
        console.warn('[chat] parse error:', err);
      }
    };

    ws.onclose = function () {
      ws = null;
      if (isGenerating) {
        isGenerating = false;
        currentAssistantEl = null;
      }
      updateStatus('disconnected');
      // Exponential backoff reconnect
      if (reconnectAttempts < MAX_RECONNECT) {
        reconnectAttempts++;
        var delay = Math.min(1000 * Math.pow(1.8, reconnectAttempts), 30000);
        setTimeout(function () { connect(currentSessionId); }, delay);
      }
    };

    ws.onerror = function () {
      updateStatus('error');
    };
  }

  // ── Send a message ───────────────────────────────────────────────────────
  function send(content) {
    if (!content || !content.trim()) return;
    if (!ws || ws.readyState !== 1) {
      connect(currentSessionId);
      updateStatus('connecting...');
      return;
    }
    if (isGenerating) return;

    content = content.trim();

    // Render user message immediately
    appendMessage('user', content);

    ws.send(JSON.stringify({
      type: 'send',
      content: content,
      session_id: currentSessionId,
    }));

    isGenerating = true;
    updateStatus('thinking');
    setSendDisabled(true);

    // Create assistant message placeholder
    startAssistantMessage();
  }

  // ── Incoming message router ──────────────────────────────────────────────
  function handleMessage(msg) {
    switch (msg.type) {

      case 'user_message_saved':
        if (msg.session_id) currentSessionId = msg.session_id;
        break;

      case 'text_start':
        break; // placeholder started in send()

      case 'text_delta':
        appendDelta(msg.delta || '');
        updateStatus('writing');
        break;

      case 'text_end':
        break;

      case 'thinking_start':
        showThinking(true);
        updateStatus('thinking');
        break;

      case 'thinking_delta':
        break; // don't show raw thinking content

      case 'thinking_end':
        showThinking(false);
        break;

      case 'tool_start':
        showToolBadge(msg.tool, msg.args || {});
        updateStatus('using ' + (msg.tool || 'tool'));
        break;

      case 'tool_end':
        break;

      case 'turn_end':
        sessionCost += msg.cost || 0;
        updateCost();
        break;

      case 'done':
        finishAssistantMessage(msg.full_text || '');
        isGenerating = false;
        setSendDisabled(false);
        updateStatus('connected');
        break;

      case 'history':
        renderHistory(msg.messages || []);
        if (msg.session_id) currentSessionId = msg.session_id;
        break;

      case 'session_created':
        if (msg.session) {
          currentSessionId = msg.session.id;
          clearMessages();
          loadSessions();
          updateSessionSelect(msg.session.id);
        }
        break;

      case 'cancelled':
        isGenerating = false;
        setSendDisabled(false);
        if (currentAssistantEl) {
          finishAssistantMessage(
            (currentAssistantEl.querySelector('.msg-content') || {}).textContent || ''
          );
        }
        updateStatus('connected');
        break;

      case 'error':
        showError(msg.message || 'Server error');
        isGenerating = false;
        setSendDisabled(false);
        updateStatus('error');
        break;
    }
  }

  // ── DOM helpers ──────────────────────────────────────────────────────────

  function getMessagesEl() {
    if (!messagesEl) messagesEl = document.getElementById('chat-messages');
    return messagesEl;
  }

  function appendMessage(role, content) {
    var el = getMessagesEl();
    if (!el) return;
    var div = document.createElement('div');
    div.className = 'chat-msg chat-msg-' + role;
    var inner = document.createElement('div');
    inner.className = 'msg-content';
    inner.innerHTML = renderMarkdown(content);
    div.appendChild(inner);
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
  }

  function startAssistantMessage() {
    var el = getMessagesEl();
    if (!el) return;
    currentAssistantEl = document.createElement('div');
    currentAssistantEl.className = 'chat-msg chat-msg-assistant';

    var content = document.createElement('div');
    content.className = 'msg-content';
    currentAssistantEl.appendChild(content);

    var tools = document.createElement('div');
    tools.className = 'msg-tools';
    currentAssistantEl.appendChild(tools);

    el.appendChild(currentAssistantEl);
    el.scrollTop = el.scrollHeight;
  }

  function appendDelta(delta) {
    if (!currentAssistantEl) startAssistantMessage();
    var contentEl = currentAssistantEl.querySelector('.msg-content');
    if (contentEl) {
      // Append raw text during streaming (re-render markdown on finish)
      contentEl.textContent = (contentEl.textContent || '') + delta;
      getMessagesEl().scrollTop = getMessagesEl().scrollHeight;
    }
  }

  function finishAssistantMessage(fullText) {
    if (!currentAssistantEl) return;
    var contentEl = currentAssistantEl.querySelector('.msg-content');
    if (contentEl) {
      contentEl.innerHTML = renderMarkdown(fullText || contentEl.textContent || '');
    }
    currentAssistantEl = null;
    var el = getMessagesEl();
    if (el) el.scrollTop = el.scrollHeight;
  }

  function showThinking(active) {
    if (!currentAssistantEl) startAssistantMessage();
    var contentEl = currentAssistantEl.querySelector('.msg-content');
    if (!contentEl) return;
    var existing = contentEl.querySelector('.thinking-indicator');
    if (active && !existing) {
      var indicator = document.createElement('div');
      indicator.className = 'thinking-indicator';
      indicator.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
      contentEl.appendChild(indicator);
      getMessagesEl().scrollTop = getMessagesEl().scrollHeight;
    } else if (!active && existing) {
      existing.remove();
    }
  }

  function showToolBadge(tool, args) {
    if (!currentAssistantEl) startAssistantMessage();
    var toolsEl = currentAssistantEl.querySelector('.msg-tools');
    if (!toolsEl) return;
    var badge = document.createElement('span');
    badge.className = 'tool-badge';
    badge.title = tool || '';

    var label = tool || 'tool';
    var t = (tool || '').toLowerCase();
    if (t === 'read') {
      label = '\uD83D\uDCC4 ' + ((args.path || '').split('/').pop() || 'file');
    } else if (t === 'bash') {
      label = '$ ' + ((args.command || '').split('\n')[0] || '').slice(0, 40);
    } else if (t === 'edit') {
      label = '\u270F\uFE0F ' + ((args.path || '').split('/').pop() || 'edit');
    } else if (t === 'write') {
      label = '\uD83D\uDCDD ' + ((args.path || '').split('/').pop() || 'write');
    } else if (t === 'grep') {
      label = '\uD83D\uDD0D ' + (args.pattern || '').slice(0, 30);
    } else if (t === 'find') {
      label = '\uD83D\uDCC2 ' + (args.pattern || '').slice(0, 30);
    } else if (t === 'delegate' || t === 'subagent') {
      label = '\uD83D\uDC65 ' + (args.agent || args.target || 'team');
    } else if (t === 'spawn_worker') {
      label = '\u26A1 ' + (args.name || 'worker');
    } else if (t === 'atlas_jobs_run') {
      label = '\uD83D\uDE80 ' + (args.job || 'job');
    }
    badge.textContent = label;
    toolsEl.appendChild(badge);
  }

  function showError(message) {
    var el = getMessagesEl();
    if (!el) return;
    var div = document.createElement('div');
    div.className = 'chat-msg chat-msg-system';
    div.textContent = '\u26A0\uFE0F ' + message;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
  }

  function renderHistory(messages) {
    clearMessages();
    // Messages come newest-first; reverse for chronological display
    var ordered = messages.slice().reverse();
    ordered.forEach(function (m) {
      appendMessage(m.role, m.content);
    });
  }

  function clearMessages() {
    var el = getMessagesEl();
    if (el) el.innerHTML = '';
    currentAssistantEl = null;
  }

  // ── Status / cost UI ────────────────────────────────────────────────────
  function updateStatus(status) {
    var el = document.getElementById('chat-connection-status');
    if (!el) return;
    el.textContent = status;
    // Update class for colour coding
    el.className = '';
    if (status === 'connected') el.className = 'connected';
    else if (status === 'thinking' || status === 'connecting...') el.className = 'thinking';
    else if (status === 'writing') el.className = 'writing';
    else if (status === 'error' || status === 'disconnected') el.className = 'error disconnected';
  }

  function updateCost() {
    var el = document.getElementById('chat-cost');
    if (el) el.textContent = '$' + sessionCost.toFixed(4);
  }

  function setSendDisabled(disabled) {
    var btn = document.getElementById('chat-send');
    if (btn) btn.disabled = disabled;
  }

  function updateSessionSelect(sessionId) {
    var select = document.getElementById('chat-session-select');
    if (select) select.value = sessionId;
  }

  // ── Session management ───────────────────────────────────────────────────
  function loadSessions() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/chat/sessions?limit=20');
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var sessions = JSON.parse(xhr.responseText);
          populateSessionSelect(sessions);
        } catch (e) {
          // ignore
        }
      }
    };
    xhr.onerror = function () { /* silently ignore */ };
    xhr.send();
  }

  function populateSessionSelect(sessions) {
    var select = document.getElementById('chat-session-select');
    if (!select) return;
    if (!sessions || !sessions.length) {
      select.innerHTML = '<option value="">No sessions</option>';
      return;
    }
    select.innerHTML = sessions.map(function (s) {
      var name = s.name || ('Chat ' + (s.id || '').slice(0, 6));
      var selected = s.id === currentSessionId ? ' selected' : '';
      return '<option value="' + escHtml(s.id) + '"' + selected + '>' + escHtml(name) + '</option>';
    }).join('');
  }

  // ── Simple markdown renderer ─────────────────────────────────────────────
  function renderMarkdown(text) {
    if (!text) return '';

    // Escape HTML first
    var html = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // Fenced code blocks (``` lang\n ... ```)
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function (_, lang, code) {
      return '<pre><code class="lang-' + escAttr(lang) + '">' + code + '</code></pre>';
    });

    // Inline code (`...`)
    html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');

    // Bold (**...**)
    html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');

    // Italic (*...*)
    html = html.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');

    // Headers
    html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');

    // Unordered lists (group consecutive <li> into <ul>)
    html = html.replace(/^(?:[-*] )(.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>[\s\S]*?<\/li>(?:\n<li>[\s\S]*?<\/li>)*)/g, '<ul>$1</ul>');

    // Links [text](url)
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

    // Paragraph breaks (two or more newlines)
    html = html.replace(/\n{2,}/g, '</p><p>');

    // Wrap in paragraph tags (only if not already block-level)
    if (!/^<(h[1-6]|ul|ol|pre|p)/.test(html)) {
      html = '<p>' + html + '</p>';
    }

    // Single newlines within paragraphs → <br>
    html = html.replace(/([^>])\n([^<])/g, '$1<br>$2');

    // Clean up empty paragraphs
    html = html.replace(/<p>\s*<\/p>/g, '');

    return html;
  }

  function escHtml(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function escAttr(s) {
    return (s || '').replace(/[^a-zA-Z0-9_-]/g, '');
  }

  // ── Public API ───────────────────────────────────────────────────────────
  return {
    init: function (sessionId) {
      currentSessionId = sessionId || null;
      messagesEl = document.getElementById('chat-messages');

      // Send button
      var sendBtn = document.getElementById('chat-send');
      var inputEl = document.getElementById('chat-input');

      if (sendBtn && inputEl) {
        sendBtn.onclick = function () {
          var val = inputEl.value;
          inputEl.value = '';
          inputEl.style.height = 'auto';
          send(val);
        };
      }

      // Keyboard: Enter = send, Shift+Enter = newline, Escape = cancel
      if (inputEl) {
        inputEl.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            var val = inputEl.value;
            inputEl.value = '';
            inputEl.style.height = 'auto';
            send(val);
          }
          if (e.key === 'Escape' && isGenerating && ws && ws.readyState === 1) {
            ws.send(JSON.stringify({ type: 'cancel', session_id: currentSessionId }));
          }
        });

        // Auto-resize textarea
        inputEl.addEventListener('input', function () {
          this.style.height = 'auto';
          this.style.height = Math.min(this.scrollHeight, 120) + 'px';
        });
      }

      // New session button
      var newBtn = document.getElementById('chat-new-session');
      if (newBtn) {
        newBtn.onclick = function () {
          if (ws && ws.readyState === 1) {
            ws.send(JSON.stringify({ type: 'new_session' }));
          } else {
            // Fallback: POST to REST API
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/chat/sessions');
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.onload = function () {
              if (xhr.status === 200) {
                try {
                  var sess = JSON.parse(xhr.responseText);
                  currentSessionId = sess.id;
                  clearMessages();
                  loadSessions();
                } catch (e) { /* ignore */ }
              }
            };
            xhr.send(JSON.stringify({}));
          }
        };
      }

      // Session selector
      var select = document.getElementById('chat-session-select');
      if (select) {
        select.onchange = function () {
          var newId = this.value;
          if (!newId) return;
          currentSessionId = newId;
          sessionCost = 0;
          updateCost();
          clearMessages();
          if (ws && ws.readyState === 1) {
            ws.send(JSON.stringify({
              type: 'history',
              session_id: currentSessionId,
              limit: 50,
            }));
          }
        };
      }

      // Connect WebSocket and load sessions
      connect(currentSessionId);
      loadSessions();
    },

    send: send,
    connect: connect,
  };
})();
