/**
 * SeismicWatch - Live camera capture, multi-image selection, GPS attachment, and submission
 */

document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const cameraStream = document.getElementById('cameraStream');
    const cameraStandbyOverlay = document.getElementById('cameraStandbyOverlay');
    const cameraControlsOverlay = document.getElementById('cameraControlsOverlay');
    const startCameraBtn = document.getElementById('startCameraBtn');
    const snapPhotoBtn = document.getElementById('snapPhotoBtn');
    const toggleCameraStreamBtn = document.getElementById('toggleCameraStreamBtn');
    const triggerFilePickerBtn = document.getElementById('triggerFilePickerBtn');
    const filePicker = document.getElementById('filePicker');
    const cameraCanvas = document.getElementById('cameraCanvas');
    
    // GPS Elements
    const gpsStatusBadge = document.getElementById('gpsStatusBadge');
    const gpsStatusText = document.getElementById('gpsStatusText');
    const latitudeInput = document.getElementById('latitudeInput');
    const longitudeInput = document.getElementById('longitudeInput');
    const accuracyInput = document.getElementById('accuracyInput');

    // Image Tray & Submission
    const imageTrayContainer = document.getElementById('imageTrayContainer');
    const imageThumbnailsTray = document.getElementById('imageThumbnailsTray');
    const trayCountText = document.getElementById('trayCountText');
    const imageCountBadge = document.getElementById('imageCountBadge');
    const clearAllImagesBtn = document.getElementById('clearAllImagesBtn');
    const submissionForm = document.getElementById('submissionForm');
    const submitReportBtn = document.getElementById('submitReportBtn');
    const submitReportBtnText = document.getElementById('submitReportBtnText');
    const submitSpinner = document.getElementById('submitSpinner');

    let currentStream = null;
    let currentFacingMode = 'environment';
    let capturedFiles = []; // Array of File objects ready for submission

    // 1. Request GPS Coordinates on Load
    function requestGPSLocation() {
        if (!navigator.geolocation) {
            updateGPSBadge(false, 'GPS not supported by browser');
            return;
        }

        navigator.geolocation.getCurrentPosition(
            (pos) => {
                const lat = pos.coords.latitude;
                const lon = pos.coords.longitude;
                const acc = Math.round(pos.coords.accuracy || 0);

                if (latitudeInput) latitudeInput.value = lat;
                if (longitudeInput) longitudeInput.value = lon;
                if (accuracyInput) accuracyInput.value = acc;

                updateGPSBadge(true, `GPS Attached: ${lat.toFixed(4)}°, ${lon.toFixed(4)}° (±${acc}m)`);
            },
            (err) => {
                console.warn('Geolocation access declined or unavailable:', err.message);
                updateGPSBadge(false, 'GPS Unavailable (optional)');
            },
            {
                enableHighAccuracy: true,
                timeout: 8000,
                maximumAge: 60000
            }
        );
    }

    function updateGPSBadge(success, text) {
        if (!gpsStatusBadge || !gpsStatusText) return;
        gpsStatusText.textContent = text;
        if (success) {
            gpsStatusBadge.className = 'badge bg-success-subtle text-success-emphasis rounded-pill px-3 py-1.5 d-flex align-items-center gap-1.5 small';
            gpsStatusBadge.innerHTML = `<i class="bi bi-geo-alt-fill text-success"></i> ${text}`;
        } else {
            gpsStatusBadge.className = 'badge bg-secondary-subtle text-secondary rounded-pill px-3 py-1.5 d-flex align-items-center gap-1.5 small';
            gpsStatusBadge.innerHTML = `<i class="bi bi-geo text-secondary"></i> ${text}`;
        }
    }

    requestGPSLocation();

    // 2. Camera Stream Management
    async function initCameraStream(facingMode = 'environment') {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            alert('Live camera streaming is not supported on this browser. You can select images from your device gallery instead.');
            return;
        }

        // Stop any running stream
        if (currentStream) {
            currentStream.getTracks().forEach(track => track.stop());
        }

        try {
            const constraints = {
                video: {
                    facingMode: facingMode,
                    width: { ideal: 1920 },
                    height: { ideal: 1080 }
                },
                audio: false
            };

            const stream = await navigator.mediaDevices.getUserMedia(constraints);
            currentStream = stream;
            currentFacingMode = facingMode;

            if (cameraStream) {
                cameraStream.srcObject = stream;
                cameraStream.play();
            }

            if (cameraStandbyOverlay) cameraStandbyOverlay.classList.add('d-none');
            if (cameraControlsOverlay) cameraControlsOverlay.classList.remove('d-none');
        } catch (err) {
            console.warn('Camera stream error:', err);
            // If environment camera failed, try user facing camera
            if (facingMode === 'environment') {
                try {
                    const fallbackStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
                    currentStream = fallbackStream;
                    if (cameraStream) {
                        cameraStream.srcObject = fallbackStream;
                        cameraStream.play();
                    }
                    if (cameraStandbyOverlay) cameraStandbyOverlay.classList.add('d-none');
                    if (cameraControlsOverlay) cameraControlsOverlay.classList.remove('d-none');
                    return;
                } catch (e2) {
                    // Ignore
                }
            }
            alert('Could not access device camera (' + err.message + '). Please use "Select Images from Device" to upload photographs.');
        }
    }

    // Auto-attempt starting camera stream if supported
    if (cameraStream && navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        initCameraStream(currentFacingMode);
    }

    if (startCameraBtn) {
        startCameraBtn.addEventListener('click', () => initCameraStream(currentFacingMode));
    }

    if (toggleCameraStreamBtn) {
        toggleCameraStreamBtn.addEventListener('click', () => {
            const nextMode = (currentFacingMode === 'environment') ? 'user' : 'environment';
            initCameraStream(nextMode);
        });
    }

    // 3. Capture Frame from Video Stream
    if (snapPhotoBtn && cameraStream && cameraCanvas) {
        snapPhotoBtn.addEventListener('click', () => {
            if (!currentStream || cameraStream.readyState < 2) {
                alert('Camera stream is still starting. Please wait a moment.');
                return;
            }

            const width = cameraStream.videoWidth || 800;
            const height = cameraStream.videoHeight || 600;

            cameraCanvas.width = width;
            cameraCanvas.height = height;
            const ctx = cameraCanvas.getContext('2d');
            ctx.drawImage(cameraStream, 0, 0, width, height);

            cameraCanvas.toBlob((blob) => {
                if (!blob) return;
                const filename = `camera_snap_${Date.now()}_${capturedFiles.length + 1}.jpg`;
                const file = new File([blob], filename, { type: 'image/jpeg' });
                addFileToTray(file);

                // Quick visual feedback flash
                if (cameraControlsOverlay) {
                    cameraControlsOverlay.classList.add('flash-effect');
                    setTimeout(() => cameraControlsOverlay.classList.remove('flash-effect'), 200);
                }
            }, 'image/jpeg', 0.92);
        });
    }

    // 4. File Picker Trigger and Listener
    if (triggerFilePickerBtn && filePicker) {
        triggerFilePickerBtn.addEventListener('click', () => filePicker.click());
    }

    if (filePicker) {
        filePicker.addEventListener('change', (e) => {
            if (e.target.files && e.target.files.length > 0) {
                Array.from(e.target.files).forEach(file => {
                    if (file.type.startsWith('image/')) {
                        addFileToTray(file);
                    }
                });
            }
            filePicker.value = ''; // Reset so same file can be re-selected if desired
        });
    }

    // 5. Image Tray Management
    function addFileToTray(file) {
        capturedFiles.push(file);
        renderImageTray();
    }

    function removeFileFromTray(index) {
        capturedFiles.splice(index, 1);
        renderImageTray();
    }

    if (clearAllImagesBtn) {
        clearAllImagesBtn.addEventListener('click', () => {
            capturedFiles = [];
            renderImageTray();
        });
    }

    function renderImageTray() {
        const count = capturedFiles.length;
        if (trayCountText) trayCountText.textContent = count;
        if (imageCountBadge) imageCountBadge.textContent = `${count} Photo${count === 1 ? '' : 's'} Ready`;

        if (count === 0) {
            if (imageTrayContainer) imageTrayContainer.classList.add('d-none');
            if (submitReportBtn) submitReportBtn.disabled = true;
            return;
        }

        if (imageTrayContainer) imageTrayContainer.classList.remove('d-none');
        if (submitReportBtn) submitReportBtn.disabled = false;
        if (!imageThumbnailsTray) return;

        imageThumbnailsTray.innerHTML = '';

        capturedFiles.forEach((file, idx) => {
            const thumbCol = document.createElement('div');
            thumbCol.className = 'position-relative border bg-light';
            thumbCol.style.width = '64px';
            thumbCol.style.height = '64px';

            const img = document.createElement('img');
            img.className = 'w-100 h-100 object-fit-cover';
            img.alt = `Photo ${idx + 1}`;

            const reader = new FileReader();
            reader.onload = (e) => { img.src = e.target.result; };
            reader.readAsDataURL(file);

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'btn btn-dark position-absolute top-0 end-0 p-0 d-flex align-items-center justify-content-center';
            removeBtn.style.width = '18px';
            removeBtn.style.height = '18px';
            removeBtn.style.fontSize = '12px';
            removeBtn.style.lineHeight = '1';
            removeBtn.innerHTML = '&times;';
            removeBtn.title = 'Remove photo';
            removeBtn.addEventListener('click', (ev) => {
                ev.preventDefault();
                removeFileFromTray(idx);
            });

            thumbCol.appendChild(img);
            thumbCol.appendChild(removeBtn);
            imageThumbnailsTray.appendChild(thumbCol);
        });
    }

    // 6. Form Submission: Package Files via DataTransfer
    if (submissionForm) {
        submissionForm.addEventListener('submit', (e) => {
            if (capturedFiles.length === 0) {
                alert('Please snap or select at least one photograph before submitting.');
                e.preventDefault();
                return;
            }

            // Sync capturedFiles into filePicker via DataTransfer
            const dt = new DataTransfer();
            capturedFiles.forEach(f => dt.items.add(f));
            filePicker.files = dt.files;

            // Stop camera stream upon submission to free device camera
            if (currentStream) {
                currentStream.getTracks().forEach(track => track.stop());
            }

            if (submitReportBtn) {
                submitReportBtn.disabled = true;
                if (submitReportBtnText) {
                    submitReportBtnText.textContent = 'Submitting...';
                }
                if (submitSpinner) {
                    submitSpinner.classList.remove('d-none');
                }
            }
        });
    }
});
