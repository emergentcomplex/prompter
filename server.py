import subprocess
import os
import json
import logging
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests
import mariadb
from mariadb import Error
from datetime import datetime
import re
import tiktoken

app = Flask(__name__)
CORS(app)

logger = logging.getLogger('PrompterApp')
logger.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

file_handler = logging.FileHandler('app.log')
file_handler.setLevel(logging.DEBUG)

formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

logger.info("Starting PrompterApp...")

config = {}
config_path = '/home/brandon/Projects/prompter/config.conf'
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

codebase_content = ""

def create_db_connection():
    try:
        connection = mariadb.connect(
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            database=DB_NAME
        )
        logger.info("Connected to MariaDB database")
        return connection
    except Error as e:
        logger.exception("Error while connecting to MariaDB")
        raise e

def init_db():
    connection = create_db_connection()
    cursor = connection.cursor()
    try:
        create_chat_history_table = """
        CREATE TABLE IF NOT EXISTS chat_history (
            id INT AUTO_INCREMENT PRIMARY KEY,
            title VARCHAR(255),
            created_at DATETIME
        )
        """
        cursor.execute(create_chat_history_table)
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
        logger.info("Database initialized and tables ensured.")
    except Error as e:
        logger.exception("Error initializing the database.")
        raise e
    finally:
        cursor.close()
        connection.close()

init_db()

def count_tokens(messages, model=MODEL):
    """
    Counts the number of tokens in the messages list based on the model's tokenization.
    """
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        logger.warning(f"Model {model} not found. Using cl100k_base encoding.")
        encoding = tiktoken.get_encoding('cl100k_base')

    if model.startswith("gpt-4"):
        tokens_per_message = 3
        tokens_per_name = 1
    elif model.startswith("gpt-3.5-turbo"):
        tokens_per_message = 4
        tokens_per_name = -1
    else:
        tokens_per_message = 3
        tokens_per_name = 1

    token_count = 0
    for message in messages:
        token_count += tokens_per_message
        for key, value in message.items():
            token_count += len(encoding.encode(value))
            if key == "name":
                token_count += tokens_per_name
    token_count += 3  # Every reply is primed with <|start|>assistant<|message|>
    return token_count

@app.route('/run_codecollector', methods=['POST'])
def run_codecollector():
    """
    Endpoint to execute the 'codecollector' command and retrieve the consolidated codebase.
    """
    global codebase_content
    logger.info("Received request to run codecollector.")
    try:
        output_file = '/home/brandon/Projects/prompter/codebase.prompt'
        logger.debug(f"Executing subprocess: {SCRIPT_NAME} {CODEBASE_DIR}")
        subprocess.run([SCRIPT_NAME, CODEBASE_DIR], check=True)
        logger.info("'codecollector' command executed successfully.")
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
    words = re.findall(r'\b\w+\b', text.lower())
    common_words = {'i', 'the', 'and', 'to', 'a', 'in', 'of', 'for', 'your', 'please', 'include', 'feature', 'new'}
    keywords = [word for word in words if word not in common_words]
    return ' '.join(keywords[:5])


@app.route('/count_tokens', methods=['POST'])
def count_tokens_route():
    """
    Endpoint to count tokens based on the full input including assistant message, conversation history, and new message.
    Expects a JSON payload with 'chat_id' (optional) and 'new_message'.
    """
    try:
        data = request.get_json()
        chat_id = data.get('chat_id')
        new_message = data.get('new_message', '').strip()
        if not new_message:
            return jsonify({'error': 'No new_message provided.'}), 400

        messages = []

        # Add assistant message with codebase_content
        if codebase_content:
            assistant_message = f"You have access to the following codebase:\n\n{codebase_content}"
            messages.append({"role": "assistant", "content": assistant_message})
            logger.debug("Added assistant message with codebase content to messages for token counting.")

        if chat_id:
            connection = create_db_connection()
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT sender, content FROM messages WHERE chat_id = ? ORDER BY timestamp ASC",
                    (chat_id,)
                )
                prior_messages = cursor.fetchall()
                for sender, content in prior_messages:
                    role = 'user' if sender == 'user' else 'assistant'
                    messages.append({"role": role, "content": content})
                logger.debug(f"Added {len(prior_messages)} prior messages from chat_id {chat_id} to messages for token counting.")
            except Error as e:
                logger.exception("Database error while fetching messages for token counting.")
                return jsonify({'error': 'Database error while fetching messages.'}), 500
            finally:
                cursor.close()
                connection.close()

        # Add the new user message
        messages.append({"role": "user", "content": new_message})
        logger.debug("Added new user message to messages for token counting.")

        # Count tokens
        token_count = count_tokens(messages, MODEL)
        logger.debug(f"Total input tokens: {token_count}")
        return jsonify({'input_token_count': token_count}), 200

    except Exception as e:
        logger.exception("Error in /count_tokens endpoint.")
        return jsonify({'error': 'Failed to count tokens.'}), 500

@app.route('/chat', methods=['POST'])
def chat():
    """
    Endpoint to handle chat messages.
    Expects a JSON payload with the user's message and optional chat_id.
    Streams the response from OpenAI to the client along with output token counts.
    """
    global codebase_content
    logger.info("Received chat request.")
    connection = None
    cursor = None
    try:
        data = request.get_json()
        user_message = data.get('message', '').strip()
        chat_id = data.get('chat_id')
        if not user_message:
            logger.warning("No message provided in the request.")
            return jsonify({'error': 'No message provided.'}), 400
        logger.debug(f"User message: {user_message}")

        connection = create_db_connection()
        cursor = connection.cursor()

        if chat_id:
            logger.debug(f"Continuing existing chat with ID: {chat_id}")
            cursor.execute("SELECT id FROM chat_history WHERE id = ?", (chat_id,))
            if cursor.fetchone() is None:
                logger.warning(f"Chat ID {chat_id} not found.")
                return jsonify({'error': 'Chat history not found.'}), 404
        else:
            title = extract_keywords(user_message) or "Untitled Chat"
            created_at = datetime.utcnow()
            cursor.execute("INSERT INTO chat_history (title, created_at) VALUES (?, ?)", (title, created_at))
            connection.commit()
            chat_id = cursor.lastrowid
            logger.info(f"Created new chat history with ID: {chat_id} and title: '{title}'")

        timestamp = datetime.utcnow()
        cursor.execute(
            "INSERT INTO messages (chat_id, sender, content, timestamp) VALUES (?, ?, ?, ?)",
            (chat_id, 'user', user_message, timestamp)
        )
        connection.commit()
        logger.debug(f"Inserted user message into chat_id {chat_id}.")

        cursor.execute(
            "SELECT sender, content FROM messages WHERE chat_id = ? ORDER BY timestamp ASC",
            (chat_id,)
        )
        prior_messages = cursor.fetchall()
        messages = []
        if codebase_content:
            assistant_message = f"You have access to the following codebase:\n\n{codebase_content}"
            messages.append({"role": "assistant", "content": assistant_message})
        for sender, content in prior_messages:
            role = 'user' if sender == 'user' else 'assistant'
            messages.append({"role": role, "content": content})

        api_payload = {
            "model": MODEL,
            "messages": messages,
            "stream": True
        }
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {API_KEY}'
        }
        logger.debug("Sending request to OpenAI API.")
        openai_response = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers=headers,
            json=api_payload,
            stream=True
        )
        if openai_response.status_code != 200:
            error_message = openai_response.json().get('error', {}).get('message', 'API request failed.')
            logger.error(f"OpenAI API request failed: {error_message}")
            return jsonify({'error': error_message}), openai_response.status_code
        logger.info("OpenAI API request successful. Streaming response to client.")

        def generate_and_store():
            bot_response = ""
            output_token_count = 0
            try:
                encoding = tiktoken.encoding_for_model(MODEL)
            except KeyError:
                encoding = tiktoken.get_encoding('cl100k_base')
            try:
                for chunk in openai_response.iter_lines():
                    if chunk:
                        if chunk.startswith(b'data: '):
                            chunk = chunk[len(b'data: '):]
                        if chunk == b'[DONE]':
                            logger.debug("Received [DONE] from OpenAI stream.")
                            break
                        data = json.loads(chunk.decode('utf-8'))
                        delta = data['choices'][0]['delta'].get('content', '')
                        yield delta
                        if delta:
                            bot_response += delta
                            tokens = encoding.encode(delta)
                            output_token_count += len(tokens)
                if bot_response.strip():
                    try:
                        bot_conn = create_db_connection()
                        bot_cursor = bot_conn.cursor()
                        bot_timestamp = datetime.utcnow()
                        bot_cursor.execute(
                            "INSERT INTO messages (chat_id, sender, content, timestamp) VALUES (?, ?, ?, ?)",
                            (chat_id, 'bot', bot_response, bot_timestamp)
                        )
                        bot_conn.commit()
                        logger.debug(f"Inserted bot message into chat_id {chat_id}.")
                        yield f"\n[TOKEN_COUNT: {output_token_count}]\n"
                    except Error as e:
                        logger.exception("Failed to insert bot message into the database.")
                        yield f"\n[Error]: Failed to store bot message: {str(e)}"
                    finally:
                        if 'bot_cursor' in locals() and bot_cursor:
                            bot_cursor.close()
                        if 'bot_conn' in locals() and bot_conn:
                            bot_conn.close()
                else:
                    logger.warning("Bot response is empty. No insertion performed.")
                    yield "\n[TOKEN_COUNT: 0]\n"
            except Exception as e:
                logger.exception("Error while streaming and storing bot response.")
                yield f"\n[Error]: {str(e)}"

        return Response(generate_and_store(), mimetype='text/plain')

    except Error as e:
        logger.exception("Database error during chat processing.")
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.exception("An unexpected error occurred during chat processing.")
        return jsonify({'error': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@app.route('/history', methods=['GET'])
def get_history():
    """
    Endpoint to retrieve a list of chat histories.
    """
    connection = None
    cursor = None
    try:
        connection = create_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, title, created_at FROM chat_history ORDER BY created_at DESC")
        histories = cursor.fetchall()
        return jsonify({'histories': histories}), 200
    except Error as e:
        logger.exception("Error fetching chat histories.")
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.exception("An unexpected error occurred while fetching chat histories.")
        return jsonify({'error': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@app.route('/history/<int:chat_id>', methods=['GET'])
def get_chat_history(chat_id):
    """
    Endpoint to retrieve messages for a specific chat history.
    """
    connection = None
    cursor = None
    try:
        connection = create_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, title, created_at FROM chat_history WHERE id = ?", (chat_id,))
        chat = cursor.fetchone()
        if not chat:
            logger.warning(f"Chat history with id {chat_id} not found.")
            return jsonify({'error': 'Chat history not found.'}), 404
        cursor.execute(
            "SELECT sender, content, timestamp FROM messages WHERE chat_id = ? ORDER BY timestamp ASC",
            (chat_id,)
        )
        messages = cursor.fetchall()
        logger.debug(f"Retrieved {len(messages)} messages for chat_id {chat_id}.")
        return jsonify({
            'chat': chat,
            'messages': messages
        }), 200
    except Error as e:
        logger.exception("Error fetching specific chat history.")
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.exception("An unexpected error occurred while fetching specific chat history.")
        return jsonify({'error': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@app.route('/count_tokens_full', methods=['POST'])
def count_tokens_full_route():
    """
    Endpoint to count the full input tokens including assistant message, conversation history, and new message.
    Expects a JSON payload with 'chat_id' (optional) and 'new_message'.
    """
    try:
        data = request.get_json()
        chat_id = data.get('chat_id')
        new_message = data.get('new_message', '').strip()
        if not new_message:
            return jsonify({'error': 'No new_message provided.'}), 400

        messages = []

        # Add assistant message with codebase_content
        if codebase_content:
            assistant_message = f"You have access to the following codebase:\n\n{codebase_content}"
            messages.append({"role": "assistant", "content": assistant_message})
            logger.debug("Added assistant message with codebase content to messages for token counting.")

        if chat_id:
            connection = create_db_connection()
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT sender, content FROM messages WHERE chat_id = ? ORDER BY timestamp ASC",
                    (chat_id,)
                )
                prior_messages = cursor.fetchall()
                for sender, content in prior_messages:
                    role = 'user' if sender == 'user' else 'assistant'
                    messages.append({"role": role, "content": content})
                logger.debug(f"Added {len(prior_messages)} prior messages from chat_id {chat_id} to messages for token counting.")
            except Error as e:
                logger.exception("Database error while fetching messages for token counting.")
                return jsonify({'error': 'Database error while fetching messages.'}), 500
            finally:
                cursor.close()
                connection.close()

        # Add the new user message
        messages.append({"role": "user", "content": new_message})
        logger.debug("Added new user message to messages for token counting.")

        # Count tokens
        token_count = count_tokens(messages, MODEL)
        logger.debug(f"Total input tokens: {token_count}")
        return jsonify({'input_token_count': token_count}), 200

    except Exception as e:
        logger.exception("Error in /count_tokens_full endpoint.")
        return jsonify({'error': 'Failed to count tokens.'}), 500

if __name__ == '__main__':
    logger.info("Running Flask app on port 5000.")
    app.run(port=5000)
