// ============================================================
//  SMILE DENTAL — app.js
//  All voice assistant logic + full motion graphics merged
// ============================================================

//  CUSTOM CURSOR 
class CustomCursor {
  constructor() {
    this.cursor = document.getElementById('cursor');
    this.trail = document.getElementById('cursorTrail');
    this.smileEl = document.getElementById('smileBlast');
    this.mouseX = -100;
    this.mouseY = -100;
    this.trailX = -100;
    this.trailY = -100;
    this.smilePool = ['😁', '😄', '🦷', '✨', '💚', '🌟', '😎', '🎉', '💎', '🔥'];

    if (!this.cursor) return;
    this._trackMouse();
    this._animTrail();
    this._hoverEffects();
    this._clickBlast();
  }

  _trackMouse() {
    document.addEventListener('mousemove', e => {
      this.mouseX = e.clientX;
      this.mouseY = e.clientY;
      this.cursor.style.left = e.clientX + 'px';
      this.cursor.style.top = e.clientY + 'px';
    });
  }

  _animTrail() {
    const tick = () => {
      this.trailX += (this.mouseX - this.trailX) * 0.12;
      this.trailY += (this.mouseY - this.trailY) * 0.12;
      if (this.trail) {
        this.trail.style.left = this.trailX + 'px';
        this.trail.style.top = this.trailY + 'px';
      }
      requestAnimationFrame(tick);
    };
    tick();
  }

  _hoverEffects() {
    const targets = document.querySelectorAll('a, button, .service-card, .team-card, .process-step, .ai-fab, .btn, .nav-links li');
    targets.forEach(el => {
      el.addEventListener('mouseenter', () => {
        this.cursor.style.width = '28px';
        this.cursor.style.height = '28px';
        this.cursor.style.background = 'var(--accent-glow)';
        if (this.trail) this.trail.style.borderColor = 'var(--accent)';
      });
      el.addEventListener('mouseleave', () => {
        this.cursor.style.width = '12px';
        this.cursor.style.height = '12px';
        this.cursor.style.background = 'var(--white)';
        if (this.trail) this.trail.style.borderColor = 'rgba(255,255,255,0.2)';
      });
    });

    document.addEventListener('mousedown', () => {
      this.cursor.style.transform = 'translate(-50%,-50%) scale(0.7)';
    });
    document.addEventListener('mouseup', () => {
      this.cursor.style.transform = 'translate(-50%,-50%) scale(1)';
    });
  }

  _clickBlast() {
    document.addEventListener('click', e => {
      // Skip blasting inside modal input area
      if (e.target.closest('.chat-input-area') || e.target.closest('#messageInputModal')) return;
      this.blast(e.clientX, e.clientY);
    });
  }

  blast(x, y) {
    if (!this.smileEl) return;
    const emoji = this.smilePool[Math.floor(Math.random() * this.smilePool.length)];
    this.smileEl.textContent = emoji;
    this.smileEl.style.left = x + 'px';
    this.smileEl.style.top = y + 'px';
    this.smileEl.style.fontSize = (1.4 + Math.random() * 0.8) + 'rem';
    this.smileEl.classList.remove('blast');
    void this.smileEl.offsetWidth; // force reflow
    this.smileEl.classList.add('blast');

    // Extra mini-bursts
    for (let i = 0; i < 6; i++) {
      const mini = document.createElement('div');
      mini.className = 'smile-blast';
      mini.textContent = this.smilePool[Math.floor(Math.random() * this.smilePool.length)];
      const ox = x + (Math.random() - 0.5) * 100;
      const oy = y + (Math.random() - 0.5) * 100;
      mini.style.cssText = `
        position:fixed;pointer-events:none;z-index:99996;
        font-size:${0.7 + Math.random() * 0.9}rem;
        left:${ox}px;top:${oy}px;
        transform:translate(-50%,-50%) scale(0);
        animation:blastAnim ${0.45 + Math.random() * 0.4}s cubic-bezier(.22,.61,.36,1) forwards;
        animation-delay:${Math.random() * 0.12}s;
      `;
      document.body.appendChild(mini);
      setTimeout(() => mini.remove(), 900);
    }
  }
}

// Make blast accessible globally (for onclick on cards)
window.smileBlastAt = function (e) {
  const x = e.clientX || window._cursorX || 0;
  const y = e.clientY || window._cursorY || 0;
  if (window._cursor) window._cursor.blast(x, y);
};

//  PARTICLE CANVAS 
class ParticleField {
  constructor() {
    this.canvas = document.getElementById('particleCanvas');
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext('2d');
    this.parts = [];
    this._resize();
    window.addEventListener('resize', () => this._resize());
    for (let i = 0; i < 90; i++) this.parts.push(new Particle(this.W, this.H));
    this._loop();
  }

  _resize() {
    this.W = this.canvas.width = window.innerWidth;
    this.H = this.canvas.height = window.innerHeight;
  }

  _loop() {
    this.ctx.clearRect(0, 0, this.W, this.H);
    this.parts.forEach(p => { p.update(this.W, this.H); p.draw(this.ctx); });
    requestAnimationFrame(() => this._loop());
  }
}

class Particle {
  constructor(W, H) { this.W = W; this.H = H; this._reset(); this.y = Math.random() * H; }
  _reset() {
    this.x = Math.random() * this.W;
    this.y = this.H + 10;
    this.r = 0.5 + Math.random() * 1.5;
    this.vx = (Math.random() - 0.5) * 0.35;
    this.vy = -(0.25 + Math.random() * 0.45);
    this.a = 0.08 + Math.random() * 0.35;
    this.life = 0;
    this.max = 180 + Math.random() * 280;
  }
  update(W, H) {
    this.x += this.vx; this.y += this.vy; this.life++;
    if (this.life > this.max || this.y < -8) { this.W = W; this.H = H; this._reset(); }
  }
  draw(ctx) {
    const ratio = this.life / this.max;
    ctx.save();
    ctx.globalAlpha = this.a * (1 - ratio);
    ctx.fillStyle = '#00ff87';
    ctx.beginPath();
    ctx.arc(this.x, this.y, this.r, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }
}

//  SCROLL REVEAL 
function initScrollReveal() {
  const els = document.querySelectorAll('.reveal-up, .reveal-right, .fade-up');
  const obs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        e.target.classList.add('visible');
        obs.unobserve(e.target);
      }
    });
  }, { threshold: 0.1, rootMargin: '0px 0px -50px 0px' });
  els.forEach(el => obs.observe(el));
}

//  PARALLAX ─
function initParallax() {
  // Mouse parallax for glow orbs
  document.addEventListener('mousemove', e => {
    const mx = (e.clientX - window.innerWidth / 2) * 0.012;
    const my = (e.clientY - window.innerHeight / 2) * 0.012;
    const g1 = document.querySelector('.glow-bg-1');
    const g2 = document.querySelector('.glow-bg-2');
    if (g1) g1.style.transform = `translate(${mx * 2}px, ${my * 2}px)`;
    if (g2) g2.style.transform = `translate(${-mx * 1.5}px, ${-my * 1.5}px)`;
    window._cursorX = e.clientX;
    window._cursorY = e.clientY;
  });

  // Scroll parallax
  const pEls = document.querySelectorAll('[data-parallax]');
  if (pEls.length > 0) {
    const render = () => {
      const sy = window.scrollY;
      const vh = window.innerHeight;
      pEls.forEach(el => {
        const speed = parseFloat(el.getAttribute('data-parallax')) || 0.15;
        const rect = el.getBoundingClientRect();
        const center = rect.top + rect.height / 2;
        const offset = (center - vh / 2) * speed;
        // Use translate3d for hardware acceleration
        el.style.transform = `translate3d(0, ${offset}px, 0)`;
      });
      requestAnimationFrame(render);
    };
    render();
  }
}

//  NAVBAR SCROLL ─
//  NAVBAR SCROLL ─
function initNavbar() {
  const nav = document.getElementById('navbar');
  if (!nav) return;
  window.addEventListener('scroll', () => {
    nav.classList.toggle('scrolled', window.scrollY > 50);
  }, { passive: true });

  //  USER DROPDOWN 
  const pill = document.getElementById('userPillToggle');
  const dropdown = document.getElementById('userProfileDropdown');
  if (pill && dropdown) {
    pill.addEventListener('click', (e) => {
      e.stopPropagation();
      const isHidden = dropdown.style.display === 'none';
      dropdown.style.display = isHidden ? 'block' : 'none';
    });
    document.addEventListener('click', () => {
      if (dropdown) dropdown.style.display = 'none';
    });
  }
}

//  NUMBER COUNTERS ─
function initCounters() {
  const obs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (!e.isIntersecting) return;
      const el = e.target;
      const raw = el.textContent.trim();

      // Match number and suffix
      const match = raw.match(/^(\d+)(.*)$/);
      if (!match) return;

      const targetNum = parseInt(match[1], 10);
      const suffix = match[2];

      const duration = 2000;
      const startTime = performance.now();

      const update = (now) => {
        const progress = Math.min((now - startTime) / duration, 1);
        const easeOut = 1 - Math.pow(1 - progress, 4); // easeOutQuart
        const currentNum = Math.floor(easeOut * targetNum);

        el.textContent = currentNum + suffix;

        if (progress < 1) {
          requestAnimationFrame(update);
        } else {
          el.textContent = raw; // Ensure final text is exact
        }
      };

      requestAnimationFrame(update);
      obs.unobserve(el);
    });
  }, { threshold: 0.1 });

  document.querySelectorAll('.hstat-num, .float-num').forEach(el => obs.observe(el));
}

//  TICKER ─
//  FAQ ACCORDION ─
function initFAQ() {
  const items = document.querySelectorAll('.faq-item');
  items.forEach(item => {
    const question = item.querySelector('.faq-question');
    question.addEventListener('click', () => {
      const isActive = item.classList.contains('active');
      items.forEach(i => i.classList.remove('active'));
      if (!isActive) item.classList.add('active');
      
      const icon = question.querySelector('i');
      items.forEach(i => {
        const qIcon = i.querySelector('.faq-question i');
        if (qIcon) qIcon.className = 'fas fa-plus';
      });
      if (!isActive && icon) icon.className = 'fas fa-minus';
    });
  });
}

//  RIPPLE (Simplified for new design) ─
function initRipple() {
  document.querySelectorAll('.btn-primary').forEach(btn => {
    btn.addEventListener('click', function (e) {
      const rect = this.getBoundingClientRect();
      const ripple = document.createElement('span');
      ripple.style.cssText = `
        position:absolute;border-radius:50%;
        width:100px;height:100px;
        background:rgba(255,255,255,0.3);
        transform:translate(-50%,-50%) scale(0);
        left:${e.clientX - rect.left}px;
        top:${e.clientY - rect.top}px;
        pointer-events:none;
        animation:rippleAnim .6s ease-out forwards;
      `;
      this.style.position = 'relative';
      this.style.overflow = 'hidden';
      this.appendChild(ripple);
      setTimeout(() => ripple.remove(), 600);
    });
  });

  if (!document.getElementById('rippleStyle')) {
    const s = document.createElement('style');
    s.id = 'rippleStyle';
    s.textContent = `@keyframes rippleAnim { to { transform:translate(-50%,-50%) scale(4);opacity:0; } }`;
    document.head.appendChild(s);
  }
}

//  MODAL 
let modalVoiceAssistant = null;

window.openBookingModal = function () {
  const modal = document.getElementById('bookingModal');
  if (!modal) return;
  modal.style.display = 'flex';
  document.body.style.overflow = 'hidden';

  if (!modalVoiceAssistant) {
    modalVoiceAssistant = new VoiceAssistant('Modal');
    setTimeout(() => modalVoiceAssistant.startSession(), 300);
  } else if (!modalVoiceAssistant.isActive) {
    modalVoiceAssistant.startSession();
  }
};

window.closeBookingModal = function () {
  const modal = document.getElementById('bookingModal');
  if (!modal) return;
  modal.style.display = 'none';
  document.body.style.overflow = '';

  if (modalVoiceAssistant && modalVoiceAssistant.isActive) {
    modalVoiceAssistant.endSession();
  }
};

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') window.closeBookingModal();
});

//  SMOOTH SCROLL HELPERS (legacy) ─
window.scrollToBooking = () => document.getElementById('booking')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
window.scrollToServices = () => document.getElementById('services')?.scrollIntoView({ behavior: 'smooth', block: 'start' });

//  VOICE ASSISTANT 
class VoiceAssistant {
  constructor(suffix = '') {
    this.sessionId = null;
    this.isActive = false;
    this.recognition = null;
    this.mediaRecorder = null;
    this.audioChunks = [];
    this.suffix = suffix;

    this.startBtn = document.getElementById('startBtn' + suffix);
    this.messageInput = document.getElementById('messageInput' + suffix);
    this.voiceBtn = document.getElementById('voiceBtn' + suffix);
    this.sendBtn = document.getElementById('sendBtn' + suffix);
    this.conversationContainer = document.getElementById('conversationContainer' + suffix);
    this.statusText = document.getElementById('statusText' + suffix);
    this.statusDot = suffix === 'Modal'
      ? document.getElementById('statusDot' + suffix)
      : document.querySelector('.status-dot');
    this.thinkingIndicator = null;

    if (!this.sendBtn || !this.messageInput) return;
    this._initEvents();
    this._initSpeech();
  }

  _initEvents() {
    if (this.startBtn) this.startBtn.addEventListener('click', () => this.startSession());
    this.sendBtn.addEventListener('click', () => this.sendMessage());
    this.voiceBtn.addEventListener('click', () => this.toggleVoiceInput());
    this.messageInput.addEventListener('keypress', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.sendMessage(); }
    });
  }

  _initSpeech() {
    // We no longer use browser SpeechRecognition. 
    // We use MediaRecorder + Deepgram API for superior multilingual support.
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      if (this.voiceBtn) this.voiceBtn.style.display = 'none';
    }
  }

  async startSession() {
    try {
      this.updateStatus('Starting session…', 'loading');
      const res = await fetch('/api/start-session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }
      });
      const data = await res.json();
      if (data.success) {
        this.sessionId = data.session_id;
        this.isActive = true;
        if (this.startBtn) this.startBtn.style.display = 'none';
        this.messageInput.disabled = false;
        this.voiceBtn.disabled = false;
        this.sendBtn.disabled = false;
        this.conversationContainer.innerHTML = '';
        this.addMessage('agent', data.message);
        this.updateStatus('Ready — type or speak', 'active');
        this.messageInput.focus();
        // Activate status dot
        if (this.statusDot) {
          this.statusDot.classList.add('active');
        }
      } else {
        this.updateStatus('Failed to start session', 'error');
      }
    } catch (err) {
      console.error(err);
      this.updateStatus('Connection error — please retry', 'error');
    }
  }

  async sendMessage() {
    const msg = this.messageInput.value.trim();
    if (!msg || !this.isActive) return;
    this.addMessage('user', msg);
    this.messageInput.value = '';
    this.updateStatus('Processing…', 'loading');
    this.showThinking();

    try {
      // Use the new streaming endpoint
      const url = `/api/send-message-stream?session_id=${this.sessionId}&message=${encodeURIComponent(msg)}`;
      const response = await fetch(url);

      this.hideThinking();
      if (!response.ok) throw new Error('Network response was not ok');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let fullResponse = '';

      // Create an empty agent message bubble to fill
      const bubble = this.addMessage('agent', '');

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        fullResponse += chunk;

        // Update bubble content in real-time
        bubble.innerHTML = '';
        fullResponse.split('\n').filter(l => l.trim()).forEach(line => {
          const p = document.createElement('p');
          p.innerHTML = line.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
          bubble.appendChild(p);
        });

        // Scroll to bottom
        this.conversationContainer.scrollTo({
          top: this.conversationContainer.scrollHeight,
          behavior: 'smooth'
        });
      }

      this.speakText(fullResponse);

      this.updateStatus('Ready — type or speak', 'active');
    } catch (err) {
      this.hideThinking();
      console.error(err);
      this.addMessage('agent', 'Connection error — please try again.');
      this.updateStatus('Connection error', 'error');
    }
  }

  async resetSession() {
    if (!this.sessionId) return;
    try {
      await fetch('/api/reset-session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: this.sessionId })
      });
      this.conversationContainer.innerHTML = '';
      this.addMessage('agent', 'Session reset. How can I help you?');
      this.updateStatus('Ready — type or speak', 'active');
      this.messageInput.value = '';
    } catch (err) { console.error(err); }
  }

  async endSession() {
    if (!this.sessionId) return;
    try {
      await fetch('/api/end-session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: this.sessionId })
      });
    } catch (err) { console.error(err); }

    this.sessionId = null;
    this.isActive = false;
    if (this.startBtn) this.startBtn.style.display = 'block';
    this.messageInput.disabled = true;
    this.voiceBtn.disabled = true;
    this.sendBtn.disabled = true;
    this.messageInput.value = '';
    this.conversationContainer.innerHTML = `
      <div class="welcome-message">
        <div class="assistant-avatar">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2.5"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/></svg>
        </div>
        <div class="message-bubble agent-message">
          <p>Hi! I'm your dental assistant. I can help you:</p>
          <ul>
            <li>📅 Book a new appointment</li>
            <li>♻️ Reschedule existing appointments</li>
            <li>❌ Cancel appointments</li>
          </ul>
          <p>Click "Start" to begin!</p>
        </div>
      </div>`;
    this.updateStatus('Ready to help', 'inactive');
    if (this.statusDot) this.statusDot.classList.remove('active');
  }

  toggleVoiceInput() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alert('Voice input is not supported in this browser.');
      return;
    }
    this.isListening ? this.stopListening() : this.startListening();
  }

  async startListening() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.mediaRecorder = new MediaRecorder(stream);
      this.audioChunks = [];

      this.mediaRecorder.ondataavailable = e => {
        if (e.data.size > 0) this.audioChunks.push(e.data);
      };

      this.mediaRecorder.onstop = () => {
        const audioBlob = new Blob(this.audioChunks, { type: 'audio/webm' });
        this.uploadAudio(audioBlob);
        // Stop all tracks to release mic
        stream.getTracks().forEach(track => track.stop());
      };

      this.isListening = true;
      if (this.voiceBtn) this.voiceBtn.classList.add('listening');
      this.updateStatus('Listening… speak now', 'listening');
      this.mediaRecorder.start();
    } catch (err) {
      console.error('Mic error:', err);
      this.updateStatus('Microphone access denied', 'error');
    }
  }

  stopListening() {
    if (this.mediaRecorder && this.isListening) {
      this.isListening = false;
      this.mediaRecorder.stop();
      if (this.voiceBtn) this.voiceBtn.classList.remove('listening');
      this.updateStatus('Transcribing...', 'loading');
    }
  }

  async uploadAudio(blob) {
    const formData = new FormData();
    formData.append('audio', blob, 'voice_msg.webm');

    try {
      const res = await fetch('/api/stt', {
        method: 'POST',
        body: formData
      });
      const data = await res.json();

      if (data.success && data.transcript) {
        this.messageInput.value = data.transcript;
        this.updateStatus('Ready — type or speak', 'active');
        this.sendMessage();
      } else {
        this.updateStatus(data.error || 'Could not understand', 'error');
        setTimeout(() => this.updateStatus('Ready — type or speak', 'active'), 3000);
      }
    } catch (err) {
      console.error('STT Upload error:', err);
      this.updateStatus('Transcription failed', 'error');
    }
  }

  addMessage(role, text) {
    const group = document.createElement('div');
    group.className = `message-group ${role}`;

    const avatar = document.createElement('div');
    avatar.className = role === 'agent' ? 'assistant-avatar' : 'user-avatar';

    if (role === 'agent') {
      avatar.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2.5"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/></svg>`;
    } else {
      avatar.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`;
    }

    const bubble = document.createElement('div');
    bubble.className = `message-bubble ${role}-message`;

    if (text) {
      text.split('\n').filter(l => l.trim()).forEach(line => {
        const p = document.createElement('p');
        p.innerHTML = line.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        bubble.appendChild(p);
      });
    }

    group.appendChild(avatar);
    group.appendChild(bubble);
    this.conversationContainer.appendChild(group);

    this.conversationContainer.scrollTo({
      top: this.conversationContainer.scrollHeight,
      behavior: 'smooth'
    });
    return bubble;
  }

  showThinking() {
    this.thinkingIndicator = document.createElement('div');
    this.thinkingIndicator.className = 'thinking-bubble';
    this.thinkingIndicator.innerHTML = `
      <div class="dot"></div>
      <div class="dot"></div>
      <div class="dot"></div>
    `;
    this.conversationContainer.appendChild(this.thinkingIndicator);
    this.conversationContainer.scrollTo({
      top: this.conversationContainer.scrollHeight,
      behavior: 'smooth'
    });
  }

  hideThinking() {
    if (this.thinkingIndicator) {
      this.thinkingIndicator.remove();
      this.thinkingIndicator = null;
    }
  }

  updateStatus(text, state) {
    if (this.statusText) this.statusText.textContent = text;
    if (!this.statusDot) return;
    this.statusDot.className = 'status-dot';
    if (state === 'active') this.statusDot.classList.add('active');
    if (state === 'listening' || state === 'loading') this.statusDot.classList.add('listening');
  }

  speakText(text) {
    if (!('speechSynthesis' in window)) return;
    window.speechSynthesis.cancel();
    const utt = new SpeechSynthesisUtterance(text);
    utt.rate = 1.0;
    utt.pitch = 1.0;
    utt.volume = 1.0;
    const voices = window.speechSynthesis.getVoices();
    // Prioritize US English female voices
    const usFemale = voices.find(v =>
      (v.lang === 'en-US' || v.lang === 'en_US') &&
      (v.name.includes('Female') || v.name.includes('Samantha') || v.name.includes('Zira') || v.name.includes('Google US English'))
    );
    if (usFemale) utt.voice = usFemale;
    else {
      const female = voices.find(v =>
        v.name.includes('Female') || v.name.includes('Samantha') || v.name.includes('Victoria')
      );
      if (female) utt.voice = female;
    }
    window.speechSynthesis.speak(utt);
  }
}

//  HERO LOAD ANIMATION ─
function initHeroReveal() {
  window.addEventListener('load', () => {
    const heroEls = document.querySelectorAll('.hero .reveal-up, .hero .reveal-right');
    heroEls.forEach((el, i) => {
      setTimeout(() => el.classList.add('visible'), 150 + i * 120);
    });
  });
}

//  INIT 
document.addEventListener('DOMContentLoaded', () => {
  if ('speechSynthesis' in window) {
    window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
  }

  initNavbar();
  initFAQ();
  initRipple();
  initScrollReveal();
  
  console.log('✦ Smile Dental — Premium UI Initialized');
});
