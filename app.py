from flask import Flask, render_template, Response, jsonify, request
import cv2
import face_recognition
import numpy as np
import os
import pandas as pd
from datetime import datetime
import base64
import threading
import queue
import time

app = Flask(__name__)

# === Config ===
BASE_DIR = r"C:\Users\GODZILLA\Desktop\B"
ENCODINGS_PATH = os.path.join(BASE_DIR, "encodings")
ATTENDANCE_FILE = os.path.join(BASE_DIR, "attendance.xlsx")

IP_CAM_URL = ""

# === Threaded Video Capture Stream class ===
class ThreadedCameraStream:
    def __init__(self, url):
        self.url = url
        self.cap = cv2.VideoCapture(url)
        self.frame = None
        self.jpeg_bytes = None
        self.success = False
        self.lock = threading.Lock()
        self.stopped = False
        self.thread = None
        
    def start(self):
        self.stopped = False
        self.thread = threading.Thread(target=self._update, daemon=True)
        self.thread.start()
        return self
        
    def _update(self):
        while not self.stopped:
            if not self.cap.isOpened():
                time.sleep(1.0)
                self.cap.open(self.url)
                continue
                
            success, frame = self.cap.read()
            if success:
                # Pre-encode to JPEG for instantaneous frame delivery
                ret, jpeg = cv2.imencode('.jpg', frame)
                with self.lock:
                    self.frame = frame
                    if ret:
                        self.jpeg_bytes = jpeg.tobytes()
                    self.success = True
            else:
                time.sleep(0.01)
                
    def read(self):
        with self.lock:
            if self.success and self.frame is not None:
                return True, self.frame.copy()
            return False, None

    def read_jpeg(self):
        with self.lock:
            if self.success and self.jpeg_bytes is not None:
                return True, self.jpeg_bytes
            return False, None
            
    def isOpened(self):
        return self.cap.isOpened()
        
    def release(self):
        self.stopped = True
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        self.cap.release()

# === Global IP Camera State ===
ip_cap = None
ip_cap_lock = threading.Lock()
current_ip_cam_url = ""

# Prevent duplicate attendance entries
daily_attendance = set()

# Initialize / Overwrite Excel File with 2 fresh sheets on startup
log_df = pd.DataFrame(columns=["Name", "Date", "Time"])
attendance_df = pd.DataFrame(columns=["Name", "Date", "First Seen Time"])
try:
    with pd.ExcelWriter(ATTENDANCE_FILE, engine='openpyxl') as writer:
        log_df.to_excel(writer, sheet_name="Log", index=False)
        attendance_df.to_excel(writer, sheet_name="Attendance", index=False)
    print("Excel attendance database started fresh successfully.")
except Exception as e:
    print(f"Error initializing fresh Excel file: {e}")

# Populate daily_attendance on startup
try:
    if os.path.exists(ATTENDANCE_FILE):
        att_df = pd.read_excel(ATTENDANCE_FILE, sheet_name="Attendance")
        today = datetime.now().strftime("%Y-%m-%d")
        today_att = att_df[att_df["Date"] == today]
        for name in today_att["Name"].dropna().unique():
            daily_attendance.add(name)
except Exception as e:
    print(f"Error loading initial daily attendance: {e}")

# === Load Face Encodings ===
known_face_encodings = []
known_face_names = []

current_name = "Unknown"
current_image_path = ""


if os.path.exists(ENCODINGS_PATH):
    for file in os.listdir(ENCODINGS_PATH):
        if file.endswith(".npy"):
            name = os.path.splitext(file)[0]
            try:
                encs = np.load(os.path.join(ENCODINGS_PATH, file))
                for enc in encs:
                    known_face_encodings.append(enc)
                    known_face_names.append(name)
                print(f"Loaded {len(encs)} encodings for {name}")
            except Exception as e:
                print(f"Error loading encoding file {file}: {e}")

# === Asynchronous Task Queue for File and Database I/O ===
bg_queue = queue.Queue()
excel_lock = threading.Lock()

def save_data_sync(name):
    with excel_lock:
        now = datetime.now()
        date = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        try:
            log_df = pd.read_excel(ATTENDANCE_FILE, sheet_name="Log")
            attendance_df = pd.read_excel(ATTENDANCE_FILE, sheet_name="Attendance")
        except Exception as e:
            print(f"Error reading Excel attendance file: {e}")
            return

        new_log = {
            "Name": name,
            "Date": date,
            "Time": time_str
        }
        log_df = pd.concat([log_df, pd.DataFrame([new_log])], ignore_index=True)

        if name not in daily_attendance:
            daily_attendance.add(name)
            new_att = {
                "Name": name,
                "Date": date,
                "First Seen Time": time_str
            }
            attendance_df = pd.concat([attendance_df, pd.DataFrame([new_att])], ignore_index=True)

        try:
            with pd.ExcelWriter(ATTENDANCE_FILE, engine='openpyxl', mode='w') as writer:
                log_df.to_excel(writer, sheet_name="Log", index=False)
                attendance_df.to_excel(writer, sheet_name="Attendance", index=False)
        except Exception as e:
            print(f"Error writing to Excel attendance file: {e}")

def bg_worker():
    global current_image_path
    while True:
        task = bg_queue.get()
        if task is None:
            break
        task_type, data = task
        try:
            if task_type == 'attendance':
                save_data_sync(data['name'])
            elif task_type == 'face_crop_filename':
                name = data['name']
                frame = data['frame']
                top, right, bottom, left = data['top'], data['right'], data['bottom'], data['left']
                face_filename = data['filename']
                
                h, w, _ = frame.shape
                top = max(0, top)
                left = max(0, left)
                bottom = min(h, bottom)
                right = min(w, right)
                
                face_crop = frame[top:bottom, left:right]
                if face_crop.size > 0:
                    static_path = os.path.join(BASE_DIR, "static")
                    os.makedirs(static_path, exist_ok=True)
                    cv2.imwrite(os.path.join(static_path, face_filename), face_crop)
        except Exception as e:
            print(f"Error in background task worker: {e}")
        finally:
            bg_queue.task_done()

# Start worker thread
threading.Thread(target=bg_worker, daemon=True).start()


@app.route('/connect_ip_cam', methods=['POST'])
def connect_ip_cam():
    global ip_cap, current_ip_cam_url, IP_CAM_URL
    data = request.get_json()
    if not data or 'url' not in data:
        return jsonify({"status": "error", "message": "No URL provided"}), 400
        
    url = data['url'].strip()
    
    # Try to open the video capture
    new_cap = ThreadedCameraStream(url).start()
    
    # Wait up to 2 seconds for first frame
    success = False
    for _ in range(20):
        time.sleep(0.1)
        success, frame = new_cap.read()
        if success:
            break
            
    if not success or not new_cap.isOpened():
        new_cap.release()
        return jsonify({"status": "error", "message": "Failed to retrieve frame from stream"}), 400
        
    # Lock and replace current cap
    with ip_cap_lock:
        if ip_cap is not None:
            ip_cap.release()
        ip_cap = new_cap
        current_ip_cam_url = url
        IP_CAM_URL = url
        
    return jsonify({"status": "success", "message": "Connected successfully"})

@app.route('/ipcam_frame')
def ipcam_frame():
    global ip_cap
    if ip_cap is None:
        return "No active IP camera connection", 404
        
    success, jpeg_bytes = ip_cap.read_jpeg()
    if not success or jpeg_bytes is None:
        return "Failed to grab frame from IP camera", 500
        
    return Response(jpeg_bytes, mimetype='image/jpeg')


# === Video Feed ===
def gen_frames():
    global current_name, current_image_path

    # Use ThreadedCameraStream to capture frames without lag
    stream = ThreadedCameraStream(IP_CAM_URL).start()
    
    frame_count = 0
    face_locations = []
    face_names = []

    try:
        while True:
            success, frame = stream.read()
            if not success:
                time.sleep(0.03)
                continue

            # Run face recognition every 5 frames
            if frame_count % 5 == 0:
                small_frame = cv2.resize(frame, (0, 0), fx=0.25, fy=0.25)
                rgb_small_frame = cv2.cvtColor(small_frame, cv2.COLOR_BGR2RGB)

                face_locations = face_recognition.face_locations(rgb_small_frame)
                face_encodings = face_recognition.face_encodings(rgb_small_frame, face_locations)

                face_names = []
                for (top, right, bottom, left), face_encoding in zip(face_locations, face_encodings):
                    matches = face_recognition.compare_faces(
                        known_face_encodings,
                        face_encoding,
                        tolerance=0.5
                    )

                    name = "Unknown"
                    if True in matches:
                        best_match_index = np.argmin(
                            face_recognition.face_distance(known_face_encodings, face_encoding)
                        )
                        name = known_face_names[best_match_index]
                        
                        # Queue Excel saving
                        bg_queue.put(('attendance', {'name': name}))
                        
                        # Generate unique filename for face crop
                        top1, right1, bottom1, left1 = top * 4, right * 4, bottom * 4, left * 4
                        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        face_filename = f"{name}_{timestamp}.jpg"
                        
                        # Update globals immediately so UI reflects the change fast
                        current_name = name
                        current_image_path = f"/static/{face_filename}"
                        
                        # Queue face crop write
                        bg_queue.put(('face_crop_filename', {
                            'name': name,
                            'frame': frame.copy(),
                            'top': top1,
                            'right': right1,
                            'bottom': bottom1,
                            'left': left1,
                            'filename': face_filename
                        }))
                    face_names.append(name)

            # Draw box on every frame using last computed locations
            for (top, right, bottom, left), name in zip(face_locations, face_names):
                top1, right1, bottom1, left1 = top * 4, right * 4, bottom * 4, left * 4
                cv2.rectangle(frame, (left1, top1), (right1, bottom1), (0, 255, 0), 2)
                cv2.putText(frame, name, (left1 + 6, bottom1 - 6),
                            cv2.FONT_HERSHEY_DUPLEX, 1.0, (0, 0, 0), 1)

            ret, buffer = cv2.imencode('.jpg', frame)
            if ret:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

            frame_count += 1
            time.sleep(0.03)  # Maintain roughly ~30 FPS
    finally:
        stream.release()


# === Routes ===
@app.route('/')
def start_screen():
    return render_template('start.html')


@app.route('/interface')
def interface():
    return render_template('interface.html')


@app.route('/video_feed')
def video_feed():
    return Response(gen_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/target_info')
def target_info():

    return jsonify({
        "name": current_name,
        "image": current_image_path if current_name != "Unknown" else ""
    })


# Metadata dictionary for target details
PEOPLE_METADATA = {
    "Nikhil": {"age": 22, "location": "HQ - Sector 7", "status": "Cleared", "threat": "LOW"},
    "ash": {"age": 24, "location": "Lab Area 3", "status": "Cleared", "threat": "LOW"},
    "thor": {"age": 1500, "location": "Asgard Gate", "status": "Cleared", "threat": "LOW"},
    "tony": {"age": 48, "location": "Stark Tower", "status": "Cleared", "threat": "LOW"},
}

def decode_base64_image(base64_str):
    if "," in base64_str:
        base64_str = base64_str.split(",")[1]
    img_data = base64.b64decode(base64_str)
    nparr = np.frombuffer(img_data, np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)

@app.route('/process_frame', methods=['POST'])
def process_frame():
    global current_name, current_image_path
    
    data = request.get_json()
    if not data or 'image' not in data:
        return jsonify({"error": "No image data provided"}), 400
        
    try:
        frame = decode_base64_image(data['image'])
        if frame is None:
            return jsonify({"error": "Failed to decode image"}), 400
            
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        face_locations = face_recognition.face_locations(rgb_frame)
        face_encodings = face_recognition.face_encodings(rgb_frame, face_locations)
        
        results = []
        
        for (top, right, bottom, left), face_encoding in zip(face_locations, face_encodings):
            matches = face_recognition.compare_faces(
                known_face_encodings,
                face_encoding,
                tolerance=0.5
            )
            
            name = "Unknown"
            if True in matches:
                best_match_index = np.argmin(
                    face_recognition.face_distance(known_face_encodings, face_encoding)
                )
                name = known_face_names[best_match_index]
                
                # Asynchronously save attendance
                bg_queue.put(('attendance', {'name': name}))
            
            # Generate unique filename for face crop
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            face_filename = f"{name}_{timestamp}.jpg"
            image_url = f"/static/{face_filename}"
            
            # Asynchronously save face crop with predetermined filename
            bg_queue.put(('face_crop_filename', {
                'name': name,
                'frame': frame.copy(),
                'top': top,
                'right': right,
                'bottom': bottom,
                'left': left,
                'filename': face_filename
            }))
            
            current_name = name
            current_image_path = image_url
            
            # Fetch details from PEOPLE_METADATA or use defaults
            metadata = PEOPLE_METADATA.get(name, {
                "age": 30 if name != "Unknown" else "--",
                "location": "Active Zone" if name != "Unknown" else "Unknown Zone",
                "status": "Authorized" if name != "Unknown" else "Intruder Alert",
                "threat": "LOW" if name != "Unknown" else "HIGH"
            })
            
            results.append({
                # Scale coordinates up to 640x480 from 320x240
                "box": {
                    "top": top * 2,
                    "right": right * 2,
                    "bottom": bottom * 2,
                    "left": left * 2
                },
                "name": name,
                "age": metadata["age"],
                "location": metadata["location"],
                "status": metadata["status"],
                "threat": metadata["threat"],
                "image": image_url
            })
            
        return jsonify({"results": results})
        
    except Exception as e:
        print(f"Error in process_frame: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/encode_single_face', methods=['POST'])
def encode_single_face():
    global known_face_encodings, known_face_names
    
    start_time = time.time()
    data = request.get_json()
    if not data or 'name' not in data or 'image' not in data:
        return jsonify({"status": "error", "message": "Missing name or image data"}), 400
        
    name = data['name'].strip()
    image_data = data['image']
    
    if not name:
        return jsonify({"status": "error", "message": "Empty employee name"}), 400
        
    try:
        # Decode base64 image
        frame = decode_base64_image(image_data)
        if frame is None:
            return jsonify({"status": "error", "message": "Failed to decode image data"}), 400
            
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # Detect faces and compute encodings
        face_locations = face_recognition.face_locations(rgb_frame)
        face_encodings = face_recognition.face_encodings(rgb_frame, face_locations)
        
        if not face_encodings:
            return jsonify({"status": "error", "message": "No face detected in the image"}), 400
            
        new_encoding = face_encodings[0]
        
        # Save encoding to file
        os.makedirs(ENCODINGS_PATH, exist_ok=True)
        npy_path = os.path.join(ENCODINGS_PATH, f"{name}.npy")
        
        if os.path.exists(npy_path):
            try:
                existing_encodings = np.load(npy_path)
                updated_encodings = np.vstack([existing_encodings, new_encoding])
            except Exception as e:
                print(f"Error loading existing encoding for {name}, overwriting: {e}")
                updated_encodings = np.array([new_encoding])
        else:
            updated_encodings = np.array([new_encoding])
            
        np.save(npy_path, updated_encodings)
        
        # Update running in-memory databases dynamically
        known_face_encodings.append(new_encoding)
        known_face_names.append(name)
        
        # Save raw image to employee archive directory
        archive_dir = os.path.join(BASE_DIR, "data", name)
        os.makedirs(archive_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        cv2.imwrite(os.path.join(archive_dir, f"{timestamp}.jpg"), frame)
        
        # If the name is not in metadata dictionary, add a default profile
        if name not in PEOPLE_METADATA:
            PEOPLE_METADATA[name] = {
                "age": 25,
                "location": "HQ Area",
                "status": "Authorized",
                "threat": "LOW"
            }
        
        time_taken = round(time.time() - start_time, 2)
        return jsonify({
            "status": "success",
            "name": name,
            "time_taken": time_taken,
            "total_active_encodings": len(known_face_names)
        })
        
    except Exception as e:
        print(f"Error in encode_single_face: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/get_attendance_logs', methods=['GET'])
def get_attendance_logs():
    try:
        with excel_lock:
            if os.path.exists(ATTENDANCE_FILE):
                df = pd.read_excel(ATTENDANCE_FILE, sheet_name="Attendance")
                df = df.fillna("")
                records = df.to_dict(orient="records")
                # Newest records first
                records.reverse()
                return jsonify({"status": "success", "records": records})
            else:
                return jsonify({"status": "success", "records": []})
    except Exception as e:
        print(f"Error reading attendance logs: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/get_registered_personnel', methods=['GET'])
def get_registered_personnel():
    personnel = []
    try:
        if os.path.exists(ENCODINGS_PATH):
            for file in os.listdir(ENCODINGS_PATH):
                if file.endswith(".npy"):
                    name = os.path.splitext(file)[0]
                    try:
                        encs = np.load(os.path.join(ENCODINGS_PATH, file))
                        personnel.append({
                            "name": name,
                            "encodings_count": len(encs)
                        })
                    except Exception:
                        pass
        return jsonify({"status": "success", "personnel": personnel})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# === Run ===
if __name__ == '__main__':
    app.run(debug=False)