/* Vivaran-VQE quiz status — poll until S2's pipeline finishes, then reload. */
(function () {
  var attemptId = Number((document.location.pathname.match(/\/quiz\/(\d+)/) || [])[1]);
  if (!attemptId) return;

  var statusText = document.getElementById('statusText');
  var errorBox = document.getElementById('quizError');
  var backRow = document.getElementById('backRow');
  var sourceLink = document.getElementById('sourceLink');

  var attempts = 0;
  var poll = setInterval(function () {
    fetch('/quiz/' + attemptId + '/status.json').then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.status === 'ready') {
          clearInterval(poll);
          window.location.href = '/quiz/' + attemptId;
        } else if (data.status === 'error') {
          clearInterval(poll);
          errorBox.style.display = 'block';
          errorBox.innerHTML =
            '<b>Quiz generation failed.</b> ' +
            '<span style="word-break:break-word;">' + (data.error || 'Unknown error') + '</span>';
          statusText.textContent = 'Try a different course, or run the quiz again.';
          backRow.style.display = 'block';
        } else {
          // still generating
          attempts += 1;
          if (attempts % 8 === 0) {
            statusText.textContent =
              'Still working on it… transcription and question-writing can take a few minutes.';
          }
        }
      })
      .catch(function () { /* transient - keep polling */ });
  }, 2500);
})();
