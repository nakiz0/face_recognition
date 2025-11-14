let streamRef = null;

function startRecognition(subject, onResult) {
  const container = document.getElementById('videoContainer');
  container.innerHTML = '';
  
  const video = document.createElement('video');
  video.autoplay = true;
  video.playsInline = true;
  video.style.width = '100%';
  video.style.height = '100%';
  container.appendChild(video);

  // Request camera with proper constraints
  const constraints = {
    video: {
      width: { ideal: 640 },
      height: { ideal: 480 },
      facingMode: 'user'
    }
  };

  navigator.mediaDevices.getUserMedia(constraints)
    .then(stream => {
      streamRef = stream;
      video.srcObject = stream;
      
      const canvas = document.createElement('canvas');
      canvas.width = 640;
      canvas.height = 480;
      const ctx = canvas.getContext('2d');
      
      let running = true;
      let recognitionInProgress = false;
      
      async function loop() {
        if (!running) return;
        
        try {
          // Draw video frame to canvas
          ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
          const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
          
          // Only send one request at a time
          if (!recognitionInProgress) {
            recognitionInProgress = true;
            
            const res = await fetch('/api/recognize', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ frame: dataUrl, subject: subject })
            });
            
            const j = await res.json();
            recognitionInProgress = false;
            
            if (j.marked) {
              running = false;
              streamRef.getTracks().forEach(t => t.stop());
              if (onResult) onResult(j);
              return;
            }
          }
        } catch (e) {
          console.error('Recognition error:', e);
          recognitionInProgress = false;
        }
        
        setTimeout(loop, 800);
      }
      
      loop();
    })
    .catch(error => {
      console.error('Camera permission error:', error);
      
      let errorMsg = '❌ Camera Access Denied';
      let errorDetail = '';
      
      if (error.name === 'NotAllowedError' || error.name === 'PermissionDeniedError') {
        errorMsg = '❌ Camera Permission Denied';
        errorDetail = 'Please allow camera access in your browser settings.';
      } else if (error.name === 'NotFoundError' || error.name === 'DevicesNotFoundError') {
        errorMsg = '❌ No Camera Found';
        errorDetail = 'Please connect a camera device.';
      } else if (error.name === 'NotReadableError' || error.name === 'TrackStartError') {
        errorMsg = '❌ Camera in Use';
        errorDetail = 'Another application is using the camera.';
      }
      
      const log = document.getElementById('log');
      if (log) {
        log.innerHTML = `<div style="color: #721c24;">${errorMsg}<br><small>${errorDetail}</small></div>`;
      }
      
      alert(`${errorMsg}\n\n${errorDetail}\n\nTo fix this:\n1. Check browser permissions\n2. Go to browser settings\n3. Allow camera access for this site\n4. Refresh the page`);
    });
}
