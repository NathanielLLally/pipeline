import subprocess
import json
from jsonargparse import auto_cli

def run_perl_script(data_dict):
    # Convert Python dictionary to a JSON string
    args = json.dumps(data_dict)

    file = args.script

    # Run the perl script, piping input and capturing output
    process = subprocess.run(
        ['perl', file],
        input=args,
        text=True,
        capture_output=True,
        check=True
    )
    
    # Parse and return the JSON response from Perl
    return json.loads(process.stdout)


if __name__ == "__main__":
    response = auto_cli(run_perl_script)

print(response)  # Output: {'message': 'Hello, Alice from Perl!', 'status': 'Success'}

