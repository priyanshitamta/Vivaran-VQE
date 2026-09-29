/* Shared register flow (learner or trainer): modal open/close + POST to /auth/register.
   On success the new user is signed in and taken to their dashboard. */
(function () {
  var modal = document.getElementById('registerModal');
  if (!modal) return;

  var errBox = document.getElementById('regError');
  var form = document.getElementById('registerForm');
  var GOV = /^[A-Za-z0-9._%+-]+@gov\.in$/i;
  var type = 'learner';

  function open() { errBox.style.display = 'none'; modal.classList.add('active'); }
  function close() { modal.classList.remove('active'); }
  function fail(msg) { errBox.textContent = msg; errBox.style.display = 'block'; errBox.scrollIntoView({ block: 'nearest' }); }

  Array.prototype.forEach.call(document.querySelectorAll('[data-open-modal]'), function (el) {
    el.addEventListener('click', open);
  });
  Array.prototype.forEach.call(document.querySelectorAll('[data-close-modal], #closeRegister'), function (el) {
    el.addEventListener('click', close);
  });
  modal.addEventListener('click', function (e) { if (e.target === modal) close(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });

  // Learner / Trainer switch
  Array.prototype.forEach.call(document.querySelectorAll('#regType button'), function (b) {
    b.addEventListener('click', function () {
      type = b.getAttribute('data-type');
      Array.prototype.forEach.call(document.querySelectorAll('#regType button'), function (x) { x.classList.toggle('active', x === b); });
      Array.prototype.forEach.call(modal.querySelectorAll('[data-for]'), function (sec) {
        sec.hidden = sec.getAttribute('data-for') !== type;
      });
      document.getElementById('regSub').textContent = type === 'trainer'
        ? 'Create your trainer account.' : 'Create your learner account (for government officials).';
      errBox.style.display = 'none';
    });
  });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    errBox.style.display = 'none';
    var body = {
      account_type: type,
      username: document.getElementById('regUsername').value.trim(),
      email: document.getElementById('regEmail').value.trim(),
      password: document.getElementById('regPassword').value
    };
    if (!body.username || !body.email || !body.password) return fail('Please fill in username, email and password.');
    if (!GOV.test(body.email)) return fail('Please use your government email ID (for example rahul@gov.in).');
    if (body.password.length < 8) return fail('Password must be at least 8 characters.');
    if (type === 'learner') {
      body.role_id = document.getElementById('regRole').value;
      body.area_of_experience = document.getElementById('regArea').value;
      if (!body.role_id) return fail('Please choose your designation.');
      if (!body.area_of_experience) return fail('Please choose your area of experience.');
    } else {
      body.specialisations = Array.prototype.map.call(
        modal.querySelectorAll('input[name=specialisations]:checked'), function (c) { return c.value; });
      if (!body.specialisations.length) return fail('Pick at least one skill you specialise in.');
    }

    var btn = document.getElementById('regSubmit');
    btn.disabled = true; btn.textContent = type === 'trainer' ? 'Creating your account…' : 'Creating your learning path…';
    fetch('/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, data: d }; });
    }).then(function (res) {
      if (res.ok) {
        window.location.href = res.data.redirect || '/';
      } else {
        btn.disabled = false; btn.textContent = 'Create account';
        fail(res.data.error || 'Registration failed. Please try again.');
      }
    }).catch(function () {
      btn.disabled = false; btn.textContent = 'Create account';
      fail('Could not reach the server. Please try again.');
    });
  });
})();
