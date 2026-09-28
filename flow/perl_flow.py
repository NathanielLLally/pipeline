use strict;
use warnings;
use Inline 'Python';




__DATA__
__Python__

from prefect import flow, tags
@flow(log_prints=True)
def hello(name: str = "Marvin") -> None:
  """Log a friendly greeting."""
  print(f"Hello, {name}!")

if __name__ == "__main__":
    # run the flow with default parameters
    with tags(
        "test"
    ):  # This is a tag that we can use to filter the flow runs in the UI
        hello()  # Logs: "Hello, Marvin!"

        # run the flow with a different input
        hello("Marvin")  # Logs: "Hello, Marvin!"

        # run the flow multiple times for different people
        crew = ["Zaphod", "Trillian", "Ford"]

        for name in crew:
            hello(name)
