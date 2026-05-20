import os
import numpy as np
import face_recognition

# Windows-style paths
BASE_DIR = r"C:\Users\GODZILLA\Desktop\B"
DATA_DIR = os.path.join(BASE_DIR, "data")
ENCODINGS_DIR = os.path.join(BASE_DIR, "encodings")

os.makedirs(ENCODINGS_DIR, exist_ok=True)

for person in os.listdir(DATA_DIR):
    person_path = os.path.join(DATA_DIR, person)
    if not os.path.isdir(person_path):
        continue

    print(f"🧠 Encoding for: {person}")
    encodings = []

    for image_name in os.listdir(person_path):
        image_path = os.path.join(person_path, image_name)
        try:
            image = face_recognition.load_image_file(image_path)
            face_locations = face_recognition.face_locations(image)
            face_encodings = face_recognition.face_encodings(image, face_locations)

            if face_encodings:
                encodings.append(face_encodings[0])
                print(f"✅ Encoded {image_name}")
            else:
                print(f"⚠️ No face found in {image_name}")
        except Exception as e:
            print(f"❌ Error processing {image_name}: {e}")

    if encodings:
        np.save(os.path.join(ENCODINGS_DIR, f"{person}.npy"), np.array(encodings))
        print(f"✅ Saved {len(encodings)} encodings for {person}\n")
    else:
        print(f"❌ No encodings found for {person}, skipping save.\n")
