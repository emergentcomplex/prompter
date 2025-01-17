import subprocess
import os
import json
import logging
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests
import mysql.connector
from mysql.connector import Error
from datetime import datetime
import re

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Setting up logging
logger = logging.getLogger('PrompterApp')
logger.setLevel(logging.DEBUG)  # Capture all log levels

# Create handlers
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)  # Console handler captures INFO and above

file_handler = logging.FileHandler('app.log')
file_handler.setLevel(logging.DEBUG)  # File handler captures DEBUG and above

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

DB_HOST = config.get('db_host')
DB_USER = config.get('db_user')
DB_PASSWORD = config.get('db_password')
DB_NAME = config.get('db_name')

if not all([API_KEY, SCRIPT_NAME, CODEBASE_DIR, DB_HOST, DB_USER, DB_PASSWORD, DB_NAME]):
    logger.critical("Missing required configurations: 'api_key', 'script_name', 'codecollector_directory', 'db_host', 'db_user', 'db_password', or 'db_name'.")
    raise ValueError("Please ensure 'api_key', 'script_name', 'codecollector_directory', 'db_host', 'db_user', 'db_password', and 'db_name' are set in config.conf.")

# Variable to store the codebase content
codebase_content = ""

# Establish MySQL connection
def create_db_connection():
    try:
        connection = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        if connection.is_connected():
            logger.info("Connected to MySQL database")
            return connection
    except Error as e:
        logger.exception("Error while connecting to MySQL")
        raise e

# Initialize database and create tables if they don't exist
def init_db():
    connection = create_db_connection()
    cursor = connection.cursor()
    # Create chat_history table
    create_chat_history_table = """
    CREATE TABLE IF NOT EXISTS chat_history (
        id INT AUTO_INCREMENT PRIMARY KEY,
        title VARCHAR(255),
        created_at DATETIME
    )
    """
    cursor.execute(create_chat_history_table)
    # Create messages table
    create_messages_table = """
    CREATE TABLE IF NOT EXISTS messages (
        id INT AUTO_INCREMENT PRIMARY KEY,
        chat_id INT,
        sender ENUM('user', 'bot'),
        content TEXT,
        timestamp DATETIME,
        FOREIGN KEY (chat_id) REFERENCES chat_history(id) ON DELETE CASCADE
    )
    """
    cursor.execute(create_messages_table)
    connection.commit()
    cursor.close()
    connection.close()
    logger.info("Database initialized and tables ensured.")

init_db()

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

def extract_keywords(text):
    """
    Extracts prominent keywords from the user message for generating chat titles.
    """
    # Simple keyword extraction using regex (for demonstration purposes)
    # In production, consider using NLP libraries like RAKE or spaCy
    words = re.findall(r'\b\w+\b', text.lower())
    common_words = {'i', 'the', 'and', 'to', 'a', 'in', 'of', 'for', 'your', 'please', 'include', 'feature', 'new'}
    keywords = [word for word in words if word not in common_words]
    return ' '.join(keywords[:5])  # Return first 5 keywords

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
        chat_id = data.get('chat_id')  # Optional, for existing chats

        if not user_message:
            logger.warning("No message provided in the request.")
            return jsonify({'error': 'No message provided.'}), 400

        logger.debug(f"User message: {user_message}")

        connection = create_db_connection()
        cursor = connection.cursor()

        if chat_id:
            # Continue existing chat
            logger.debug(f"Continuing existing chat with ID: {chat_id}")
            # Verify if chat_id exists
            cursor.execute("SELECT id FROM chat_history WHERE id = %s", (chat_id,))
            if cursor.fetchone() is None:
                logger.warning(f"Chat ID {chat_id} not found.")
                return jsonify({'error': 'Chat history not found.'}), 404
        else:
            # Create new chat history
            title = extract_keywords(user_message) or "Untitled Chat"
            created_at = datetime.utcnow()
            cursor.execute("INSERT INTO chat_history (title, created_at) VALUES (%s, %s)", (title, created_at))
            connection.commit()
            chat_id = cursor.lastrowid
            logger.info(f"Created new chat history with ID: {chat_id} and title: '{title}'")

        # Insert user message
        timestamp = datetime.utcnow()
        cursor.execute(
            "INSERT INTO messages (chat_id, sender, content, timestamp) VALUES (%s, %s, %s, %s)",
            (chat_id, 'user', user_message, timestamp)
        )
        connection.commit()
        logger.debug(f"Inserted user message into chat_id {chat_id}.")

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

        # Insert bot response as it streams
        def generate_and_store():
            bot_response = ""
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
                        bot_response += delta
                        yield delta
                # After streaming is done, insert bot response into DB
                if bot_response.strip():  # Ensure non-empty response
                    try:
                        # Establish a new connection for insertion
                        bot_conn = create_db_connection()
                        bot_cursor = bot_conn.cursor()
                        bot_timestamp = datetime.utcnow()
                        bot_cursor.execute(
                            "INSERT INTO messages (chat_id, sender, content, timestamp) VALUES (%s, %s, %s, %s)",
                            (chat_id, 'bot', bot_response, bot_timestamp)
                        )
                        bot_conn.commit()
                        logger.debug(f"Inserted bot message into chat_id {chat_id}.")
                    except Exception as e:
                        logger.exception("Failed to insert bot message into the database.")
                        yield f"\n[Error]: Failed to store bot message: {str(e)}"
                    finally:
                        if 'bot_cursor' in locals() and bot_cursor:
                            bot_cursor.close()
                        if 'bot_conn' in locals() and bot_conn.is_connected():
                            bot_conn.close()
                else:
                    logger.warning("Bot response is empty. No insertion performed.")
            except Exception as e:
                logger.exception("Error while streaming and storing bot response.")
                yield f"\n[Error]: {str(e)}"

        return Response(generate_and_store(), mimetype='text/plain')

    except Exception as e:
        logger.exception("An unexpected error occurred during chat processing.")
        return jsonify({'error': str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'connection' in locals() and connection.is_connected():
            connection.close()

@app.route('/history', methods=['GET'])
def get_history():
    """
    Endpoint to retrieve a list of chat histories.
    """
    try:
        connection = create_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, title, created_at FROM chat_history ORDER BY created_at DESC")
        histories = cursor.fetchall()
        return jsonify({'histories': histories}), 200
    except Exception as e:
        logger.exception("Error fetching chat histories.")
        return jsonify({'error': str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'connection' in locals() and connection.is_connected():
            connection.close()

@app.route('/history/<int:chat_id>', methods=['GET'])
def get_chat_history(chat_id):
    """
    Endpoint to retrieve messages for a specific chat history.
    """
    try:
        connection = create_db_connection()
        cursor = connection.cursor(dictionary=True)
        # Verify chat_id exists
        cursor.execute("SELECT id, title, created_at FROM chat_history WHERE id = %s", (chat_id,))
        chat = cursor.fetchone()
        if not chat:
            logger.warning(f"Chat history with id {chat_id} not found.")
            return jsonify({'error': 'Chat history not found.'}), 404
        # Fetch messages
        cursor.execute(
            "SELECT sender, content, timestamp FROM messages WHERE chat_id = %s ORDER BY timestamp ASC",
            (chat_id,)
        )
        messages = cursor.fetchall()
        logger.debug(f"Retrieved {len(messages)} messages for chat_id {chat_id}.")
        return jsonify({
            'chat': chat,
            'messages': messages
        }), 200
    except Exception as e:
        logger.exception("Error fetching specific chat history.")
        return jsonify({'error': str(e)}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'connection' in locals() and connection.is_connected():
            connection.close()

if __name__ == '__main__':
    logger.info("Running Flask app on port 5000.")
    app.run(port=5000)
