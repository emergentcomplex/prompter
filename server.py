import subprocess
import os
import json
import logging
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Setting up logging
logger = logging.getLogger('PrompterApp')
logger.setLevel(logging.DEBUG)  # Set to DEBUG to capture all levels of log messages

# Create handlers
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)  # Console handler will capture INFO and above

file_handler = logging.FileHandler('app.log')
file_handler.setLevel(logging.DEBUG)  # File handler will capture DEBUG and above

# Create formatters and add them to handlers
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

# Add handlers to the logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)

logger.info("Starting PrompterApp...")

# Load configuration
config = {}
config_path = 'config.conf'

if os.path.exists(config_path):
    logger.debug(f"Loading configuration from {config_path}")
    with open(config_path, 'r') as f:
        for line in f:
            if '=' in line:
                key, value = line.strip().split('=', 1)
                config[key.strip()] = value.strip()
    logger.info("Configuration loaded successfully.")
else:
    logger.critical("config.conf not found. Please create one with the required configurations.")
    raise FileNotFoundError("config.conf not found. Please create one with the required configurations.")

API_KEY = config.get('api_key')
MODEL = config.get('model', 'gpt-3.5-turbo')
SCRIPT_NAME = config.get('script_name', 'codecollector')
CODEBASE_DIR = config.get('codecollector_directory')

if not all([API_KEY, SCRIPT_NAME, CODEBASE_DIR]):
    logger.critical("Missing required configurations: 'api_key', 'script_name', or 'codecollector_directory'.")
    raise ValueError("Please ensure 'api_key', 'script_name', and 'codecollector_directory' are set in config.conf.")

# Variable to store the codebase content
codebase_content = ""

@app.route('/run_codecollector', methods=['POST'])
def run_codecollector():
    """
    Endpoint to execute the 'codecollector' command and retrieve the consolidated codebase.
    """
    global codebase_content
    logger.info("Received request to run codecollector.")

    try:
        # Execute the 'codecollector' command
        output_file = 'codebase.prompt'  # Adjust if needed
        logger.debug(f"Executing subprocess: {SCRIPT_NAME} {CODEBASE_DIR}")

        # Run the command
        subprocess.run([SCRIPT_NAME, CODEBASE_DIR], check=True)
        logger.info("'codecollector' command executed successfully.")

        # Read the output file
        if not os.path.exists(output_file):
            logger.error(f"Output file '{output_file}' not found.")
            return jsonify({'error': f"Output file '{output_file}' not found."}), 500

        with open(output_file, 'r', encoding='utf-8') as f:
            codebase_content = f.read()
        logger.info(f"Codebase content loaded from '{output_file}'.")

        return jsonify({'message': 'Codebase loaded successfully.'}), 200

    except subprocess.CalledProcessError as e:
        logger.exception("Command execution failed.")
        return jsonify({'error': f"Command execution failed: {str(e)}"}), 500
    except Exception as ex:
        logger.exception("An unexpected error occurred.")
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
                    logger.debug("Received [DONE] from OpenAI stream.")
                    break
                data = json.loads(chunk.decode('utf-8'))
                delta = data['choices'][0]['delta'].get('content', '')
                yield delta
    except Exception as e:
        logger.exception("Error while generating stream.")
        yield f"\n[Error]: {str(e)}"

@app.route('/chat', methods=['POST'])
def chat():
    """
    Endpoint to handle chat messages.
    Expects a JSON payload with the user's message.
    Streams the response from OpenAI to the client.
    """
    global codebase_content
    logger.info("Received chat request.")

    try:
        data = request.get_json()
        user_message = data.get('message', '').strip()

        if not user_message:
            logger.warning("No message provided in the request.")
            return jsonify({'error': 'No message provided.'}), 400

        logger.debug(f"User message: {user_message}")

        # Initialize conversation with codebase as part of the user's message
        if codebase_content:
            # Adding a separator for clarity
            combined_message = f"You have access to the following codebase:\n\n{codebase_content}\n\nUser Query:\n{user_message}"
            logger.debug("Combined user message with codebase content.")
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

        logger.debug("Sending request to OpenAI API.")
        openai_response = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers=headers,
            json=api_payload,
            stream=True  # Enable streaming in requests
        )

        if openai_response.status_code != 200:
            error_message = openai_response.json().get('error', {}).get('message', 'API request failed.')
            logger.error(f"OpenAI API request failed: {error_message}")
            return jsonify({'error': error_message}), openai_response.status_code

        logger.info("OpenAI API request successful. Streaming response to client.")
        # Stream the response to the client
        return Response(generate_stream(openai_response), mimetype='text/plain')

    except Exception as e:
        logger.exception("An unexpected error occurred during chat processing.")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    logger.info("Running Flask app on port 5000.")
    app.run(port=5000)