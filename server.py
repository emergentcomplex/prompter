# File: /home/brandon/Projects/prompter/server.js

import subprocess
import os
import json
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Load configuration
config = {}
config_path = 'config.conf'

if os.path.exists(config_path):
    with open(config_path, 'r') as f:
        for line in f:
            if '=' in line:
                key, value = line.strip().split('=', 1)
                config[key.strip()] = value.strip()
else:
    raise FileNotFoundError("config.conf not found. Please create one with the required configurations.")

API_KEY = config.get('api_key')
MODEL = config.get('model', 'gpt-3.5-turbo')
SCRIPT_NAME = config.get('script_name', 'codecollector')
CODEBASE_DIR = config.get('codecollector_directory')

if not all([API_KEY, SCRIPT_NAME, CODEBASE_DIR]):
    raise ValueError("Please ensure 'api_key', 'script_name', and 'codecollector_directory' are set in config.conf.")

# Variable to store the codebase content
codebase_content = ""

@app.route('/run_codecollector', methods=['POST'])
def run_codecollector():
    """
    Endpoint to execute the 'codecollector' command and retrieve the consolidated codebase.
    """
    global codebase_content
    try:
        # Execute the 'codecollector' command
        output_file = 'codebase.prompt'  # Adjust if needed

        # Run the command
        subprocess.run([SCRIPT_NAME, CODEBASE_DIR], check=True)

        # Read the output file
        if not os.path.exists(output_file):
            return jsonify({'error': f"Output file '{output_file}' not found."}), 500

        with open(output_file, 'r', encoding='utf-8') as f:
            codebase_content = f.read()

        return jsonify({'message': 'Codebase loaded successfully.'}), 200

    except subprocess.CalledProcessError as e:
        return jsonify({'error': f"Command execution failed: {e}"}), 500
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500

def generate_stream(openai_response):
    """
    Generator function to yield chunks of data from OpenAI's streaming response.
    """
    try:
        for chunk in openai_response.iter_lines():
            if chunk:
                # OpenAI streams data with 'data: ' prefix
                if chunk.startswith(b'data: '):
                    chunk = chunk[len(b'data: '):]
                if chunk == b'[DONE]':
                    break
                data = json.loads(chunk.decode('utf-8'))
                delta = data['choices'][0]['delta'].get('content', '')
                yield delta
    except Exception as e:
        yield f"\n[Error]: {str(e)}"

@app.route('/chat', methods=['POST'])
def chat():
    """
    Endpoint to handle chat messages.
    Expects a JSON payload with the user's message.
    Streams the response from OpenAI to the client.
    """
    global codebase_content
    try:
        data = request.get_json()
        user_message = data.get('message', '').strip()

        if not user_message:
            return jsonify({'error': 'No message provided.'}), 400

        # Initialize conversation with codebase as part of the user's message
        if codebase_content:
            # Adding a separator for clarity
            combined_message = f"You have access to the following codebase:\n\n{codebase_content}\n\nUser Query:\n{user_message}"
        else:
            combined_message = user_message

        # Prepare the API request payload
        api_payload = {
            "model": MODEL,
            "messages": [
                {"role": "user", "content": combined_message}
            ],
            "stream": True  # Enable streaming
        }

        # Make the API request to OpenAI with streaming
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {API_KEY}'
        }

        openai_response = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers=headers,
            json=api_payload,
            stream=True  # Enable streaming in requests
        )

        if openai_response.status_code != 200:
            error_message = openai_response.json().get('error', {}).get('message', 'API request failed.')
            return jsonify({'error': error_message}), openai_response.status_code

        # Stream the response to the client
        return Response(generate_stream(openai_response), mimetype='text/plain')

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(port=5000)
