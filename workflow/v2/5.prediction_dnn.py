# -*- coding: utf-8 -*-

import logging
logging.basicConfig(level=logging.INFO)

try:
    from aces.config import Config
    from aces.model_builder import ModelBuilder
    from aces.utils import TFUtils
except ModuleNotFoundError:
    print("ModuleNotFoundError: Attempting to import from parent directory.")
    import os, sys
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

    from aces.config import Config
    from aces.model_builder import ModelBuilder
    from aces.utils import TFUtils

import json
import os
import tensorflow as tf
import numpy as np
import subprocess


OUTPUT_IMAGE_FILE = str(Config.MODEL_DIR / "prediction" / f"{Config.OUTPUT_NAME}.TFRecord")
if not os.path.exists(str(Config.MODEL_DIR / "prediction")): os.mkdir(str(Config.MODEL_DIR / "prediction"))

OUTPUT_GCS_PATH = f"gs://{Config.GCS_BUCKET}/prediction/{Config.OUTPUT_NAME}.TFRecord"

ls = f"sudo gsutil ls gs://{Config.GCS_BUCKET}/{Config.GCS_IMAGE_DIR}/"
files_list = subprocess.check_output(ls, shell=True)
files_list = files_list.decode("utf-8")
files_list = files_list.split("\n")

# Get only the files generated by the image export.
exported_files_list = [s for s in files_list if Config.GCS_IMAGE_PREFIX in s]

# Get the list of image files and the JSON mixer file.
image_files_list = []
json_file = None
for f in exported_files_list:
    if f.endswith(".tfrecord.gz"):
        image_files_list.append(f)
    elif f.endswith(".json"):
        json_file = f

# Make sure the files are in the right order.
image_files_list.sort()

physical_devices = TFUtils.configure_memory_growth()
logging.info(f"Using last model for inference.\nLoading model from {str(Config.MODEL_DIR)}/trained-model")
this_model = tf.keras.models.load_model(f"{str(Config.MODEL_DIR)}/trained-model")

logging.info(this_model.summary())

cat = f"gsutil cat {json_file}"
read_t = subprocess.check_output(cat, shell=True)
read_t = read_t.decode("utf-8")

# Get a single string w/ newlines from the IPython.utils.text.SList
mixer = json.loads(read_t)

# Get relevant info from the JSON mixer file.
patch_width = mixer["patchDimensions"][0]
patch_height = mixer["patchDimensions"][1]
patches = mixer["totalPatches"]
patch_dimensions_flat = [patch_width * patch_height, 1]

# Get set up for prediction.
if Config.KERNEL_BUFFER:
    x_buffer = Config.KERNEL_BUFFER[0] // 2
    y_buffer = Config.KERNEL_BUFFER[1] // 2

    buffered_shape = [
        Config.PATCH_SHAPE[0] + Config.KERNEL_BUFFER[0],
        Config.PATCH_SHAPE[1] + Config.KERNEL_BUFFER[1],
    ]
else:
    x_buffer = 0
    y_buffer = 0
    buffered_shape = Config.PATCH_SHAPE

if Config.USE_ELEVATION:
    Config.FEATURES.extend(["elevation", "slope"])


if Config.USE_S1:
    Config.FEATURES.extend(["vv_asc_before", "vh_asc_before", "vv_asc_during", "vh_asc_during",
                            "vv_desc_before", "vh_desc_before", "vv_desc_during", "vh_desc_during"])

print(f"Config.FEATURES: {Config.FEATURES}")

image_columns = [
    tf.io.FixedLenFeature(shape=patch_dimensions_flat, dtype=tf.float32) for k in Config.FEATURES
]

image_features_dict = dict(zip(Config.FEATURES, image_columns))

def parse_image(example_proto):
    return tf.io.parse_single_example(example_proto, image_features_dict)


def toTupleImage(inputs):
    inputsList = [inputs.get(key) for key in Config.FEATURES]
    stacked = tf.stack(inputsList, axis=0)

    stacked = tf.transpose(stacked, [1, 2, 0])
    return stacked

# Create a dataset from the TFRecord file(s) in Cloud Storage.
image_dataset = tf.data.TFRecordDataset(image_files_list, compression_type="GZIP")

# Parse the data into tensors, one long tensor per patch.
image_dataset = image_dataset.map(parse_image, num_parallel_calls=5)

# Break our long tensors into many little ones.
image_dataset = image_dataset.flat_map(
  lambda features: tf.data.Dataset.from_tensor_slices(features)
)

# Turn the dictionary in each record into a tuple without a label.
image_dataset = image_dataset.map(
  lambda data_dict: (tf.transpose(list(data_dict.values())), )
)

# image_dataset = image_dataset.map(toTupleImage)

# for (None, in_shape)
image_dataset = image_dataset.batch(patch_width * patch_height)

for inputs in image_dataset.take(1):
    print("inputs", inputs)

# Perform inference.
print("Running predictions...")
# Run prediction in batches, with as many steps as there are patches.
predictions = this_model.predict(image_dataset, steps=patches, verbose=1)

# Instantiate the writer.
print("Writing predictions...")
writer = tf.io.TFRecordWriter(OUTPUT_IMAGE_FILE)

# Every patch-worth of predictions we"ll dump an example into the output
# file with a single feature that holds our predictions. Since our predictions
# are already in the order of the exported data, the patches we create here
# will also be in the right order.
# patch = [[]]
patch = [[], [], [], [], [], []]
cur_patch = 1
for i, prediction in enumerate(predictions):
    patch[0].append(int(np.argmax(prediction)))
    patch[1].append(prediction[0][0])
    patch[2].append(prediction[0][1])
    patch[3].append(prediction[0][2])
    patch[4].append(prediction[0][3])
    patch[5].append(prediction[0][4])


    if i == 0:
        print(f"prediction.shape: {prediction.shape}")

    if (len(patch[0]) == patch_width * patch_height):
        if cur_patch % 100 == 0:
            print("Done with patch " + str(cur_patch) + " of " + str(patches) + "...")

        example = tf.train.Example(
            features=tf.train.Features(
                feature={
                "prediction": tf.train.Feature(
                    int64_list=tf.train.Int64List(
                        value=patch[0])),
                "cropland_etc": tf.train.Feature(
                    float_list=tf.train.FloatList(
                        value=patch[1])),
                "rice": tf.train.Feature(
                    float_list=tf.train.FloatList(
                        value=patch[2])),
                "forest": tf.train.Feature(
                    float_list=tf.train.FloatList(
                        value=patch[3])),
                "urban": tf.train.Feature(
                    float_list=tf.train.FloatList(
                        value=patch[4])),
                "others_etc": tf.train.Feature(
                    float_list=tf.train.FloatList(
                        value=patch[5])),
                }
            )
        )

        # Write the example to the file and clear our patch array so it"s ready for
        # another batch of class ids
        writer.write(example.SerializeToString())
        patch = [[], [], [], [], [], []]
        cur_patch += 1

writer.close()

# upload to gcp
upload_to_gcp = f"sudo gsutil cp {OUTPUT_IMAGE_FILE} {OUTPUT_GCS_PATH}"
result = subprocess.check_output(upload_to_gcp, shell=True)
print(f"uploading classified image to earth engine: {result}")

# upload to earth engine asset
upload_image = f"earthengine upload image --asset_id={Config.EE_OUTPUT_ASSET}/{Config.OUTPUT_NAME} --pyramiding_policy=mode {OUTPUT_GCS_PATH} {json_file}"
result = subprocess.check_output(upload_image, shell=True)
print(f"uploading classified image to earth engine: {result}")
