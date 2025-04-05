import os
import json
import re
import logging as log
import openai
from pathlib import Path  # Using pathlib for easier path handling

# Initialize logger for this module
log = log.getLogger(__name__)

# Define path to the system prompt template file relative to this script
# Assumes system_prompt.txt is in the same directory as command_generator.py
_PROMPT_TEMPLATE_PATH = Path(__file__).parent / "system_prompt.txt"

class CommandGenerator:
    """
    Generates FFmpeg commands using an LLM based on user prompts and system context.

    Loads the system prompt template from an external file and interacts with the
    OpenAI API to produce a command string and explanation in JSON format.
    """

    def __init__(self, model: str = "gpt-4o-mini", temperature: float = 0.1, verbose: bool = False):
        """
        Initializes the CommandGenerator.

        Args:
            model: The name of the OpenAI model to use (e.g., "gpt-4o-mini").
                   Passed from configuration.
            temperature: The sampling temperature for the LLM.
            verbose: Controls whether to include traceback details in logs.
        """
        self.model = model
        self.temperature = temperature
        self.verbose = verbose
        try:
            with open(_PROMPT_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
                self.system_prompt_template = f.read()
            log.debug(f"Successfully loaded system prompt template from {_PROMPT_TEMPLATE_PATH}")
        except FileNotFoundError:
            log.error(f"System prompt template file not found at: {_PROMPT_TEMPLATE_PATH}", exc_info=self.verbose)
            # Provide a fallback or raise a critical error
            raise FileNotFoundError(f"Required system prompt file missing: {_PROMPT_TEMPLATE_PATH}")
        except Exception as e:
            log.error(f"Error loading system prompt template: {e}", exc_info=self.verbose)
            raise

        log.debug(f"CommandGenerator initialized with model: {self.model}, temperature: {self.temperature}")

    def _format_file_context(self, system_context: dict[str, str]) -> str:
        """
        Formats the file context string for the system prompt.

        Args:
            system_context: Dictionary containing system/environment details.

        Returns:
            A formatted string describing the file context.
        """
        file_context_lines = ["\nFILE CONTEXT:"]
        explicit_file = system_context.get("explicit_input_file")
        detected_files = system_context.get("detected_files_in_directory")
        cwd = system_context.get("current_directory", ".")  # Get CWD for context message

        if explicit_file:
            file_context_lines.append(f"- Explicit input file provided: '{explicit_file}' (Use this exact path)")
        if detected_files:
            relative_files_for_prompt = []
            for f_abs in detected_files:
                try:
                    # Attempt to get relative path for brevity in prompt
                    rel_path = os.path.relpath(f_abs, cwd)
                    # Heuristic: Use relative path if shorter and not complex
                    if len(rel_path) < len(f_abs) and '../' not in rel_path[3:]:
                        relative_files_for_prompt.append(rel_path)
                    else:
                        relative_files_for_prompt.append(f_abs)  # Fallback to absolute
                except ValueError:
                    # Files might be on different drives (Windows)
                    log.warning(f"Could not create relative path for '{f_abs}' from CWD '{cwd}'. Using absolute path in prompt context.", exc_info=self.verbose)
                    relative_files_for_prompt.append(f_abs)  # Use absolute path

            files_list_str = ", ".join([f"'{f}'" for f in relative_files_for_prompt])
            # Include CWD in message for clarity
            file_context_lines.append(f"- Media files found in directory '{cwd}': {files_list_str}")
            if detected_files:  # Add note only if files were actually detected
                # Clarify that absolute paths should be used in commands if needed
                file_context_lines.append(f"- Note: In the generated command, use full absolute paths for these files where necessary (e.g., '{detected_files[0]}' ...).")

        # Add the summary message if it exists and provides unique info
        summary_msg = system_context.get("file_context_message", "")
        # Check if the summary is already covered by the explicit/detected file lines
        if summary_msg and not explicit_file and not detected_files:
            file_context_lines.append(f"- Additional context: {summary_msg}")
        elif not explicit_file and not detected_files:
            # Be explicit if no files found/specified
            file_context_lines.append(f"- No specific input file provided or common media files detected in the directory '{cwd}'.")

        # Join lines only if there's more than the header
        return "\n".join(file_context_lines) if len(file_context_lines) > 1 else ""

    def _prepare_llm_messages(self, conversation_history: list[dict[str, str]], system_context: dict[str, str]) -> list[dict[str, str]]:
        """
        Prepares the list of messages to send to the LLM API.

        Args:
            conversation_history: The history of the conversation.
            system_context: Dictionary containing system/environment details.

        Returns:
            A list of message dictionaries for the API call.

        Raises:
            ValueError: If the system context dictionary is missing required keys
                        for prompt formatting.
        """
        # Format the dynamic file context part
        file_context_str = self._format_file_context(system_context)

        # Construct the final system prompt
        try:
            formatted_system_prompt = self.system_prompt_template.format(
                os_info=system_context.get('os_info', 'Unknown'),
                os_type=system_context.get('os_type', 'Unknown'),
                shell=system_context.get('shell', 'Unknown'),
                ffmpeg_version=system_context.get('ffmpeg_version', 'Unknown'),
                ffmpeg_executable_path=system_context.get('ffmpeg_executable_path', 'ffmpeg'),
                current_directory=system_context.get("current_directory", "."),
                file_context=file_context_str
            )
        except KeyError as e:
            log.error(f"Missing key in system_context for prompt formatting: {e}", exc_info=self.verbose)
            # Raise a clear ValueError to be handled upstream
            raise ValueError(f"System context dictionary is missing required key: {e}") from e

        messages = [{"role": "system", "content": formatted_system_prompt}]
        # Filter out empty user messages if any crept in
        valid_history = [msg for msg in conversation_history if msg.get("content")]
        messages.extend(valid_history)  # Add user prompts, assistant responses, errors etc.

        return messages

    def _call_llm_api(self, messages: list[dict[str, str]]) -> str:
        """
        Calls the OpenAI Chat Completion API.

        Args:
            messages: The list of messages formatted for the API.

        Returns:
            The raw content string from the LLM response.

        Raises:
            openai.* errors: Propagates API-specific errors for handling upstream.
            Exception: Catches and re-raises unexpected errors during the API call.
        """
        log.debug(f"Sending messages to LLM (model: {self.model}): {json.dumps(messages, indent=2)}")
        try:
            # NOTE: Assumes openai.api_key is set globally in the main application
            response = openai.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                response_format={"type": "json_object"}  # Request JSON output
            )
            content = response.choices[0].message.content
            log.debug(f"LLM raw choice content: {content}")

            if not content:
                log.warning("LLM returned empty content.")
                # Return structure indicating failure but parsable by clean_json_response/json.loads
                return json.dumps({"explanation": ["LLM returned empty content."], "command": ""})

            return content

        # Specific OpenAI errors are NOT caught here - they will propagate up
        # to be handled by the main application logic (e.g., toast.py)
        except Exception as e:
            log.error("Unexpected error during OpenAI API call:", exc_info=self.verbose)
            raise

    def clean_json_response(self, response_str: str) -> str:
        """
        Cleans common markdown formatting issues around JSON responses from LLMs.
        Attempts to extract the outermost JSON object. Robustly handles variations.

        Args:
            response_str: The raw string response from the LLM.

        Returns:
            A string potentially containing a clean JSON object, or the original
            string if cleaning fails. JSON validity is checked later.
        """
        if not isinstance(response_str, str):
            log.warning(f"clean_json_response received non-string input: {type(response_str)}")
            return ""  # Return empty string for non-string input

        response_str = response_str.strip()

        # 1. Remove markdown code blocks (```json ... ``` or ``` ... ```)
        # Using DOTALL to match across newlines, IGNORECASE for 'json' tag
        match = re.match(r"^\s*```(?:json)?\s*(.*)\s*```\s*$", response_str, re.DOTALL | re.IGNORECASE)
        if match:
            response_str = match.group(1).strip()

        # 2. Find the first '{' and the last '}' to define the JSON boundaries
        # This helps trim potential leading/trailing non-JSON text LLMs sometimes add
        try:
            start_index = response_str.index("{")
            end_index = response_str.rindex("}") + 1
            response_str = response_str[start_index:end_index]
        except ValueError:
            # If no '{' or '}' found, it's likely not a valid JSON object string.
            log.warning("Could not find JSON object boundaries '{...}' in LLM response after cleaning markdown.")
            # Return the processed string; parsing will fail later if it's not JSON.
            return response_str

        # 3. Optional: Further cleaning (e.g., removing trailing commas) could be added here,
        # but standard json.loads often handles minor issues. Rely on it for validation.

        return response_str

    def generate_command(self, conversation_history: list[dict[str, str]], system_context: dict[str, str]) -> str:
        """
        Generates the FFmpeg command JSON string using the LLM.

        Orchestrates the process: prepares messages, calls the API, and returns
        the raw JSON content string. Expects caller to handle API errors & parse JSON.

        Args:
            conversation_history: The history of the conversation (user prompts, prior results/errors).
            system_context: Dictionary containing system, file, and environment details.

        Returns:
            A raw string potentially containing the JSON response from the LLM.
            This string should be cleaned and parsed by the caller.

        Raises:
            ValueError: If system context is missing required keys for prompt formatting.
            openai.* errors: Propagates API-specific errors from the API call.
            Exception: Propagates unexpected errors from API call or message prep.
            FileNotFoundError: If the system prompt template file cannot be loaded.
        """
        # 1. Prepare messages using helper methods
        # ValueError from missing keys in system_context can be raised here
        messages = self._prepare_llm_messages(conversation_history, system_context)

        # 2. Call the LLM API using a helper method
        # This call can raise various openai.* errors or other exceptions,
        # which are intentionally *not* caught here but propagated upwards.
        raw_llm_response = self._call_llm_api(messages)

        # 3. Return the raw response string (caller will clean and parse)
        return raw_llm_response