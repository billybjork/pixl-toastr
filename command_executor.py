import subprocess
import shlex
import time
import logging
from typing import Tuple, Union, List
import platform
import shutil

class CommandExecutor:
    def __init__(self, max_retries: int = 2):
        self.max_retries = max_retries

    def _looks_like_shell_script(self, command: str) -> bool:
        """
        Basic heuristic to check if a command string likely requires shell interpretation.
        Checks for shell keywords, loops, pipes, redirects, variable expansion etc.
        This is not exhaustive but covers common cases generated by the LLM.
        """
        command_lower = command.lower().strip()
        # Keywords often starting a command requiring shell=True
        if command_lower.startswith(('for ', 'while ', 'if ', 'case ')):
            return True
        # Common shell operators indicating complexity
        if any(op in command for op in [';', '&&', '||', '|', '>', '<', '`']):
            return True
        # Shell variable expansions (simple check)
        # Match ${...} or $VAR type patterns (but avoid simple $ signs)
        if '$' in command and ( '{' in command or command.split('$')[-1].split()[0].isalnum() ):
             return True
        # Match %VAR% on Windows
        if '%' in command and platform.system() == "Windows":
             # Basic check for %...% pattern
             parts = command.split('%')
             if len(parts) > 2 and parts[1].isalnum(): # Check if there's something between %
                 return True

        # Default to False if none of the above are strongly indicative
        return False

    def run_command(self, command: str) -> Tuple[bool, str]:
        """
        Run the given command. Uses shell=True if it looks like a script,
        otherwise uses shlex.split for safety.
        Returns (success, combined_output).
        """
        logging.info(f"Executing: {command}")
        use_shell = self._looks_like_shell_script(command)
        command_to_run: Union[str, List[str]] # Type hint for clarity
        error_prefix = "" # Store specific errors like FileNotFoundError

        try:
            if use_shell:
                logging.debug("Executing command using shell=True")
                command_to_run = command # Pass the whole string
            else:
                logging.debug("Executing command using shlex.split and shell=False")
                command_to_run = shlex.split(command)
                if not command_to_run:
                    return False, "Empty command after shlex.split"
                # Check executable existence only when not using shell=True implicitly
                executable_path = shutil.which(command_to_run[0])
                if not executable_path:
                     error_prefix = f"Error: Command executable '{command_to_run[0]}' not found in PATH."
                     logging.error(error_prefix)
                     # Let subprocess.run raise the FileNotFoundError for consistency
                else:
                     # Optionally log the found path
                     logging.debug(f"Found executable for '{command_to_run[0]}': {executable_path}")

            # Execute using subprocess.run
            completed_process = subprocess.run(
                command_to_run,
                shell=use_shell, # Set based on detection
                capture_output=True,
                text=True,
                check=False, # We check returncode manually
                # Using shell=True implicitly searches PATH. Explicit executable needed?
                # executable=None # Maybe set to detected shell if needed? e.g., get_default_shell()
                timeout=300 # Add a timeout (e.g., 5 minutes) for safety
            )

            # Combine stdout and stderr for output context
            combined_output = ""
            if completed_process.stdout:
                 combined_output += f"Stdout:\n{completed_process.stdout.strip()}\n"
            if completed_process.stderr:
                 combined_output += f"Stderr:\n{completed_process.stderr.strip()}"
            combined_output = combined_output.strip()


            if completed_process.returncode == 0:
                logging.debug(f"Command successful. Output:\n{combined_output or '<No output>'}")
                return True, combined_output
            else:
                error_message = f"Command failed with exit code {completed_process.returncode}."
                # Prepend specific error if found earlier
                if error_prefix: error_message = f"{error_prefix}\n{error_message}"
                logging.error(error_message)
                if combined_output: logging.error(f"Output:\n{combined_output}")
                # Return the combined output which includes stderr for error analysis
                return False, f"{error_message}\n{combined_output}"

        except FileNotFoundError as e:
            # This error occurs if the executable isn't found *when shell=False*
            # or potentially if the shell itself isn't found when shell=True
            err_msg = f"Error: FileNotFoundError during command execution. Command or shell not found? Details: {e}"
            logging.error(err_msg)
            return False, err_msg
        except ValueError as e:
            # shlex.split can raise ValueError for unterminated quotes
            err_msg = f"Error splitting command string (check quoting): {e}\nCommand: {command}"
            logging.error(err_msg)
            return False, err_msg
        except subprocess.TimeoutExpired:
            err_msg = f"Error: Command timed out after 300 seconds.\nCommand: {command}"
            logging.error(err_msg)
            return False, err_msg
        except Exception as e:
            # Catch other potential subprocess errors
            err_msg = f"Subprocess execution error: {str(e)}"
            logging.exception("Subprocess execution error details:") # Log traceback
            return False, err_msg

    def execute_with_retries(self, command: str) -> Tuple[bool, str]:
        """
        Execute the command, retrying on failure with exponential backoff.
        Confirmation and dry_run are handled in the main loop now.
        """
        attempt = 0
        max_exec_retries = self.max_retries # Use configured max_retries
        last_error_output = "No error output captured."

        while attempt < max_exec_retries:
            attempt += 1
            logging.debug(f"Execution attempt #{attempt} of {max_exec_retries}")

            success, output = self.run_command(command)

            if success:
                return True, output # Return success and any output
            else:
                last_error_output = output # Keep track of the last error
                # Don't retry immediately if it's a clear structural or setup error
                # or a shell pattern matching error
                no_retry_errors = [
                    "Error splitting command string",
                    "executable not found",
                    "FileNotFoundError",
                    "no matches found",
                    "timed out" # Don't retry timeouts immediately
                ]
                if any(err_text in output for err_text in no_retry_errors):
                     logging.warning(f"Command failed due to specific error, not retrying execution. Error text contained: {output[:200]}...")
                     break # Exit retry loop

                # If retries remain, wait and log
                if attempt < max_exec_retries:
                    sleep_time = 2 ** attempt
                    logging.warning(f"Command failed. Retrying execution in {sleep_time} seconds...")
                    time.sleep(sleep_time)
                else:
                     logging.error(f"Command failed after maximum {max_exec_retries} execution retries.")

        # If loop finishes without success
        return False, last_error_output