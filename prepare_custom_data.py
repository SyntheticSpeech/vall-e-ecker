import os
import subprocess
import csv

def m4a_to_wav():
    # Ensure the output folder exists, or create it if it doesn't
    src_folder = '/Users/apple/Desktop/coquiTTS/fine-tune_dataset/new_m4as'
    dst_folder = '/Users/apple/Desktop/Practicum/self-collect-dataset-3/'
    os.makedirs(dst_folder, exist_ok=True)
    # Loop through all files in the input folder
    for filename in os.listdir(src_folder):
        if filename.endswith('.aifc'):
            input_file = os.path.join(src_folder, filename)
            file_index = os.path.splitext(filename)[0]
            output_file = os.path.join(dst_folder, f'{file_index}.wav')
            # Use subprocess to run FFmpeg to convert the file
            cmd = ['ffmpeg', '-i', input_file, '-ac', '1', '-ar', '24000', output_file]
            try:
                subprocess.run(cmd, check=True)
                print(f"Converted {input_file} to {output_file}")
            except subprocess.CalledProcessError as e:
                print(f"Failed to convert {input_file}: {e}")

def create_txt_transcriptions():
    # Open the input text file with transcriptions
    input_file = "/Users/apple/Desktop/coquiTTS/fine-tune_dataset/new_m4as/new_train.txt"
    dest_folder = '/Users/apple/Desktop/Practicum/self-collect-dataset-3/'
    os.makedirs(dest_folder, exist_ok=True)
    # Open the file for reading
    with open(input_file, "r", encoding="utf-8") as file:
        # Read each line
        for line_number, line in enumerate(file, start=1):
            line = line.split('|')[2]
            line = line[:len(line)-1]
            # Clean and create a filename for the new text file
            filename = f"{dest_folder}/{line_number}.txt"
            with open(filename, "w", encoding="utf-8") as output_file:
                output_file.write(line)
            # filename = f"{dest_folder}/{line_number}.normalized.txt"
            # with open(filename, "w", encoding='utf-8') as output_file:
            #     output_file.write(line)


if __name__ == '__main__':
    m4a_to_wav()
    create_txt_transcriptions()

            