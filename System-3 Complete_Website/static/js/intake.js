/* Vivaran-VQE intake form — builds dropdowns/skills from S1 metadata (META) and
   submits to S3's /recommendation, which relays to System 1's register API. */
(function () {
  var META = window.S3_META || {};
  if (!META.roles) return; // s1_down banner already shown server-side

  var $ = function (id) { return document.getElementById(id); };
  var errBox = $('intakeError');
  var rolesByDesignation = {};

  // ---- designation -> departments map -----------------------------------
  (META.roles || []).forEach(function (role) {
    (rolesByDesignation[role.designation] = rolesByDesignation[role.designation] || []).push(role.department);
  });

  function fillSelect(el, values, label) {
    el.innerHTML = '<option value="">' + label + '</option>';
    values.slice().sort().forEach(function (v) {
      var opt = document.createElement('option');
      opt.value = v;
      opt.textContent = v;
      el.appendChild(opt);
    });
  }

  // ---- static dropdowns --------------------------------------------------
  var designationSel = $('designation');
  var deptSel = $('department');
  var designations = Object.keys(rolesByDesignation);
  fillSelect(designationSel, designations, 'Select designation');
  fillSelect($('currentAssignment'), META.assignments || [], 'Select assignment');
  fillSelect($('educationalQualifications'), META.qualifications || [], 'Select qualification');

  designationSel.addEventListener('change', function () {
    var depts = (rolesByDesignation[designationSel.value] || []).slice().sort();
    fillSelect(deptSel, depts, 'Select department');
  });

  // ---- self-rated skills grid (all skills, one level select each) -------
  var grid = $('skillsGrid');
  (META.skills || []).forEach(function (skill) {
    var wrap = document.createElement('div');

    var lbl = document.createElement('div');
    lbl.textContent = skill.skill_name;
    lbl.style.cssText = 'font-weight:600; font-size:.9rem; margin-bottom:.25rem;';

    var sel = document.createElement('select');
    sel.dataset.skillId = skill.skill_id;
    sel.className = 'skill-level';
    [['', 'Not rated'], ['1', 'Beginner'], ['2', 'Intermediate'], ['3', 'Advanced']].forEach(function (pair) {
      var opt = document.createElement('option');
      opt.value = pair[0];
      opt.textContent = pair[1];
      sel.appendChild(opt);
    });

    wrap.appendChild(lbl);
    wrap.appendChild(sel);
    grid.appendChild(wrap);
  });

  // ---- previous iGOT courses --------------------------------------------
  var hasPrev = $('hasPreviousTrainings');
  var prevSection = $('previousTrainingsSection');
  var courseList = $('courseList');

  hasPrev.addEventListener('change', function () {
    var show = hasPrev.value === 'yes';
    prevSection.style.display = show ? 'block' : 'none';
    if (show && courseList.children.length === 0) addCourseRow();
  });

  function addCourseRow() {
    var row = document.createElement('div');
    row.className = 'course-row';

    var sel = document.createElement('select');
    var ph = document.createElement('option');
    ph.value = '';
    ph.textContent = 'Select course';
    sel.appendChild(ph);
    (META.courses || []).slice().sort(function (a, b) { return a.course_id.localeCompare(b.course_id); })
      .forEach(function (c) {
        var opt = document.createElement('option');
        opt.value = c.course_id;
        opt.textContent = c.course_id + ' — ' + c.course_title;
        sel.appendChild(opt);
      });

    var del = document.createElement('button');
    del.type = 'button';
    del.className = 'del-row-btn';
    del.title = 'Remove course';
    del.textContent = '✕';
    del.addEventListener('click', function () { row.remove(); });

    row.appendChild(sel);
    row.appendChild(del);
    courseList.appendChild(row);
  }

  var addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'add-row-btn';
  addBtn.textContent = '+ Add course';
  addBtn.style.fontSize = '.9rem';
  addBtn.addEventListener('click', addCourseRow);
  courseList.parentNode.insertBefore(addBtn, courseList.nextSibling);

  function showError(message) {
    errBox.textContent = message;
    errBox.style.display = 'block';
    errBox.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // ---- submit ------------------------------------------------------------
  $('intakeForm').addEventListener('submit', function (e) {
    e.preventDefault();
    errBox.style.display = 'none';

    var name = $('name').value.trim();
    var designation = designationSel.value;
    var department = deptSel.value;
    var yearsRaw = $('workExperience').value;
    var years = parseInt(yearsRaw, 10);

    var problems = [];
    if (!name) problems.push('Full Name');
    if (!designation) problems.push('Designation');
    if (!department) problems.push('Department');
    if (yearsRaw === '' || isNaN(years) || years < 0) problems.push('Work Experience (years)');
    if (problems.length) { showError('Please fill in these fields first: ' + problems.join(', ') + '.'); return; }

    var role = (META.roles || []).find(function (r) {
      return r.designation === designation && r.department === department;
    });
    if (!role) { showError('No role is defined for ' + designation + ' / ' + department + '. Try another combination.'); return; }

    var selfRated = {};
    grid.querySelectorAll('.skill-level').forEach(function (sel) {
      if (sel.value) selfRated[sel.dataset.skillId] = parseInt(sel.value, 10);
    });
    if (!Object.keys(selfRated).length) {
      showError('Rate at least one skill so the engine can compare your portfolio against the role.');
      return;
    }

    var previousTrainings = [];
    courseList.querySelectorAll('select').forEach(function (sel) {
      if (sel.value) previousTrainings.push(sel.value);
    });

    var payload = {
      name: name,
      role_id: role.role_id,
      designation: designation,
      department: department,
      current_assignment: $('currentAssignment').value || null,
      educational_qualifications: $('educationalQualifications').value || null,
      work_experience_years: years,
      previous_trainings: previousTrainings,
      self_rated_skills: selfRated
    };

    var btn = $('intakeSubmit');
    var original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Analysing…';

    fetch('/recommendation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (resp) {
      return resp.json().then(function (data) { return { ok: resp.ok, data: data }; });
    }).then(function (res) {
      if (res.ok) {
        window.location.href = res.data.redirect;
      } else {
        showError(res.data.error || 'The engine rejected this profile. Please review and try again.');
        btn.disabled = false;
        btn.textContent = original;
      }
    }).catch(function () {
      showError('Something went wrong while submitting. Please try again.');
      btn.disabled = false;
      btn.textContent = original;
    });
  });
})();
