import json
from tqdm import tqdm
import argparse
import os
import sys
from utils import (
    content_to_json,
    print_response,
    print_log_cost,
    load_accumulated_cost,
    save_accumulated_cost,
    make_openai_client,
    normalize_completion,
    get_completion_message,
    load_paper_content,
)
from task_manifest import (
    parse_task_manifest_mapping,
    safe_write_text,
    save_task_manifest,
    validate_task_path,
)

parser = argparse.ArgumentParser()

parser.add_argument('--paper_name',type=str)
parser.add_argument('--provider',type=str, required=True)
parser.add_argument('--gpt_version',type=str, required=True)
parser.add_argument('--paper_format',type=str, default="JSON", choices=["JSON", "LaTeX", "Markdown"])
parser.add_argument('--pdf_json_path', type=str) # json format
parser.add_argument('--pdf_latex_path', type=str) # latex format
parser.add_argument('--pdf_markdown_path', type=str) # markdown format
parser.add_argument('--domain', type=str, default="general", choices=["general", "statistics"])
parser.add_argument('--output_dir',type=str, default="")

args    = parser.parse_args()

paper_name = args.paper_name
gpt_version = args.gpt_version
provider_id = args.provider
paper_format = args.paper_format
pdf_json_path = args.pdf_json_path
pdf_latex_path = args.pdf_latex_path
pdf_markdown_path = args.pdf_markdown_path
domain = args.domain
output_dir = args.output_dir
client = make_openai_client(provider_id, gpt_version)
os.makedirs(output_dir, exist_ok=True)

paper_content = load_paper_content(
    paper_format,
    json_path=pdf_json_path,
    latex_path=pdf_latex_path,
    markdown_path=pdf_markdown_path,
)

plan_msg = [
        {'role': "system", "content": f"""You are an expert researcher and strategic planner with a deep understanding of experimental design and reproducibility in scientific research. 
You will receive a research paper in {paper_format} format. 
Your task is to create a detailed and efficient plan to reproduce the experiments and methodologies described in the paper.
This plan should align precisely with the paper's methodology, experimental setup, and evaluation metrics. 

Instructions:

1. Align with the Paper: Your plan must strictly follow the methods, datasets, model configurations, hyperparameters, and experimental setups described in the paper.
2. Be Clear and Structured: Present the plan in a well-organized and easy-to-follow format, breaking it down into actionable steps.
3. Prioritize Efficiency: Optimize the plan for clarity and practical implementation while ensuring fidelity to the original experiments."""},
        {"role": "user",
         "content" : f"""## Paper
{paper_content}

## Task
1. We want to reproduce the method described in the attached paper. 
2. The authors did not release any official code, so we have to plan our own implementation.
3. Before writing any code, please outline a comprehensive plan that covers:
   - Key details from the paper's **Methodology**.
   - Important aspects of **Experiments**, including dataset requirements, experimental settings, hyperparameters, or evaluation metrics.
4. The plan should be as **detailed and informative** as possible to help us write the final code later.

## Requirements
- You don't need to provide the actual code yet; focus on a **thorough, clear strategy**.
- If something is unclear from the paper, mention it explicitly.

## Instruction
The response should give us a strong roadmap, making it easier to write the code later."""}]

file_list_msg = [
        {"role": "user", "content": """Your goal is to create a concise, usable, and complete software system design for reproducing the paper's method. Use appropriate open-source libraries and keep the overall architecture simple.
             
Based on the plan for reproducing the paper’s main method, please design a concise, usable, and complete software system. 
Keep the architecture simple and make effective use of open-source libraries.

-----

## Format Example
[CONTENT]
{
    "Implementation approach": "We will ... ,
    "File list": [
        "main.py",  
        "dataset_loader.py", 
        "model.py",  
        "trainer.py",
        "evaluation.py" 
    ],
    "Data structures and interfaces": "\nclassDiagram\n    class Main {\n        +__init__()\n        +run_experiment()\n    }\n    class DatasetLoader {\n        +__init__(config: dict)\n        +load_data() -> Any\n    }\n    class Model {\n        +__init__(params: dict)\n        +forward(x: Tensor) -> Tensor\n    }\n    class Trainer {\n        +__init__(model: Model, data: Any)\n        +train() -> None\n    }\n    class Evaluation {\n        +__init__(model: Model, data: Any)\n        +evaluate() -> dict\n    }\n    Main --> DatasetLoader\n    Main --> Trainer\n    Main --> Evaluation\n    Trainer --> Model\n",
    "Program call flow": "\nsequenceDiagram\n    participant M as Main\n    participant DL as DatasetLoader\n    participant MD as Model\n    participant TR as Trainer\n    participant EV as Evaluation\n    M->>DL: load_data()\n    DL-->>M: return dataset\n    M->>MD: initialize model()\n    M->>TR: train(model, dataset)\n    TR->>MD: forward(x)\n    MD-->>TR: predictions\n    TR-->>M: training complete\n    M->>EV: evaluate(model, dataset)\n    EV->>MD: forward(x)\n    MD-->>EV: predictions\n    EV-->>M: metrics\n",
    "Anything UNCLEAR": "Need clarification on the exact dataset format and any specialized hyperparameters."
}
[/CONTENT]

## Nodes: "<node>: <type>  # <instruction>"
- Implementation approach: <class 'str'>  # Summarize the chosen solution strategy.
- File list: typing.List[str]  # Only need relative paths. ALWAYS write a main.py or app.py here.
- Data structures and interfaces: typing.Optional[str]  # Use mermaid classDiagram code syntax, including classes, method(__init__ etc.) and functions with type annotations, CLEARLY MARK the RELATIONSHIPS between classes, and comply with PEP8 standards. The data structures SHOULD BE VERY DETAILED and the API should be comprehensive with a complete design.
- Program call flow: typing.Optional[str] # Use sequenceDiagram code syntax, COMPLETE and VERY DETAILED, using CLASSES AND API DEFINED ABOVE accurately, covering the CRUD AND INIT of each object, SYNTAX MUST BE CORRECT.
- Anything UNCLEAR: <class 'str'>  # Mention ambiguities and ask for clarifications.

## Constraint
Format: output wrapped inside [CONTENT][/CONTENT] like the format example, nothing else.

## Action
Follow the instructions for the nodes, generate the output, and ensure it follows the format example."""}
    ]

task_list_msg = [
        {'role': 'user', 'content': """Your goal is break down tasks according to PRD/technical design, generate a task list, and analyze task dependencies. 
You will break down tasks, analyze dependencies.
             
You outline a clear PRD/technical design for reproducing the paper’s method and experiments. 

Now, let's break down tasks according to PRD/technical design, generate a task list, and analyze task dependencies.
The Logic Analysis should not only consider the dependencies between files but also provide detailed descriptions to assist in writing the code needed to reproduce the paper.

-----

## Format Example
[CONTENT]
{
    "Required packages": [
        "numpy==1.21.0",
        "torch==1.9.0"  
    ],
    "Required Other language third-party packages": [
        "No third-party dependencies required"
    ],
    "Logic Analysis": [
        [
            "data_preprocessing.py",
            "DataPreprocessing class ........"
        ],
        [
            "trainer.py",
            "Trainer ....... "
        ],
        [
            "dataset_loader.py",
            "Handles loading and ........"
        ],
        [
            "model.py",
            "Defines the model ......."
        ],
        [
            "evaluation.py",
            "Evaluation class ........ "
        ],
        [
            "main.py",
            "Entry point  ......."
        ]
    ],
    "Task list": [
        "dataset_loader.py", 
        "model.py",  
        "trainer.py", 
        "evaluation.py",
        "main.py"  
    ],
    "Full API spec": "openapi: 3.0.0 ...",
    "Shared Knowledge": "Both data_preprocessing.py and trainer.py share ........",
    "Anything UNCLEAR": "Clarification needed on recommended hardware configuration for large-scale experiments."
}

[/CONTENT]

## Nodes: "<node>: <type>  # <instruction>"
- Required packages: typing.Optional[typing.List[str]]  # Provide required third-party packages in requirements.txt format.(e.g., 'numpy==1.21.0').
- Required Other language third-party packages: typing.List[str]  # List down packages required for non-Python languages. If none, specify "No third-party dependencies required".
- Logic Analysis: typing.List[typing.List[str]]  # Provide a list of files with the classes/methods/functions to be implemented, including dependency analysis and imports. Include as much detailed description as possible.
- Task list: typing.List[str]  # Break down the tasks into a list of filenames, prioritized based on dependency order. The task list must include the previously generated file list.
- Full API spec: <class 'str'>  # Describe all APIs using OpenAPI 3.0 spec that may be used by both frontend and backend. If front-end and back-end communication is not required, leave it blank.
- Shared Knowledge: <class 'str'>  # Detail any shared knowledge, like common utility functions or configuration variables.
- Anything UNCLEAR: <class 'str'>  # Mention any unresolved questions or clarifications needed from the paper or project scope.

## Constraint
Format: output wrapped inside [CONTENT][/CONTENT] like the format example, nothing else.

## Action
Follow the node instructions above, generate your output accordingly, and ensure it follows the given format example."""}]

# config
config_msg = [
        {'role': 'user', 'content': """You write elegant, modular, and maintainable code. Adhere to Google-style guidelines.

Based on the paper, plan, design specified previously, follow the "Format Example" and generate the code. 
Extract the training details from the above paper (e.g., learning rate, batch size, epochs, etc.), follow the "Format example" and generate the code. 
DO NOT FABRICATE DETAILS — only use what the paper provides.

You must write `config.yaml`.

ATTENTION: Use '##' to SPLIT SECTIONS, not '#'. Your output format must follow the example below exactly.

-----

# Format Example
## Code: config.yaml
```yaml
## config.yaml
training:
  learning_rate: ...
  batch_size: ...
  epochs: ...
...
```

-----

## Code: config.yaml
"""
    }]

if domain == "statistics":
    statistics_scope_msg = {
        "role": "user",
        "content": """## Domain-Specific Reproduction Scope
This is a statistics/econometrics-style paper reproduction. The code repository must focus on the mathematical/statistical model, parameter estimation methods, numerical simulation, Monte Carlo experiments, and paper-specific accuracy metrics.

Do not design a deep-learning training pipeline unless the paper explicitly requires one. Do not create trainer.py or dataset_loader.py by default. Only create data-loading code if the paper has an empirical data application that truly requires it.

Use R as the implementation language for statistics/econometrics papers.

Preferred repository structure:
- config.yaml: experiment settings, parameter grids, sample sizes, seeds, estimator choices, simulation repetitions, and metric settings.
- simulation.R: concise data generation exactly following the paper's DGP and scenario settings. Put model-specific random generation here instead of creating model.R.
- estimators.R: concise implementations of MLE, GMM, EM, Bayesian, method-of-moments, penalized estimation, or other estimation methods described by the paper. Put estimator-specific parameter checks and numerical safeguards here instead of creating utils.R.
- experiments.R: Monte Carlo loop, optional empirical-data fitting workflow, metric definitions, metric formulas in short comments, metric computation, result aggregation, and table-ready summaries. Put metrics here instead of creating metrics.R.
- main.R: command-line entry point that starts with numbered comment sections for assumptions and output locations, creates output directories, records seed choices, runs experiments, and saves outputs. Put directory creation and table saving here instead of creating utils.R.

Do not create model.R, metrics.R, or utils.R by default.
""",
    }
    plan_msg.append(statistics_scope_msg)

    file_list_msg = [
        {
            "role": "user",
            "content": """Your goal is to create a concise, usable, and complete software system design for reproducing the paper's statistical model, estimation methods, numerical simulations, and reported metrics.

This is a statistics/econometrics-oriented reproduction. Use R as the implementation language. Prefer concise estimator/simulation/experiment scripts over broad software architecture. Do not generate model.R, metrics.R, utils.R, trainer.py, dataset_loader.py, or Python files unless the paper explicitly requires them.

-----

## Format Example
[CONTENT]
{
    "Implementation approach": "We will implement the paper's simulation DGP, parameter estimation methods, Monte Carlo or empirical experiment workflow, and reported metrics in concise R scripts. Model-specific formulas are placed directly in simulation.R or estimators.R, and metrics are defined and computed inside experiments.R.",
    "File list": [
        "config.yaml",
        "simulation.R",
        "estimators.R",
        "experiments.R",
        "main.R"
    ],
    "Data structures and interfaces": "\\nclassDiagram\\n    class config_yaml {\\n        +sample_sizes\\n        +n_replications\\n        +random_seed\\n        +true_parameters\\n        +estimator_names\\n        +output_dir\\n    }\\n    class simulation_R {\\n        +simulate_paper_dgp(config, scenario, seed)\\n    }\\n    class estimators_R {\\n        +estimate_parameters(data, config, method)\\n        +paper_objective(params, data, config)\\n    }\\n    class experiments_R {\\n        +run_single_replication(config, scenario, replication_id, seed)\\n        +run_monte_carlo(config)\\n        +compute_paper_metrics(estimates, truth)\\n        +summarize_results(results)\\n    }\\n    class main_R {\\n        +load_config(path)\\n        +run_and_save()\\n    }\\n    main_R --> config_yaml\\n    main_R --> experiments_R\\n    experiments_R --> simulation_R\\n    experiments_R --> estimators_R\\n",
    "Program call flow": "\\nsequenceDiagram\\n    participant M as main.R\\n    participant C as config.yaml\\n    participant EX as experiments.R\\n    participant S as simulation.R\\n    participant E as estimators.R\\n    M->>M: show top comments for assumptions and output locations\\n    M->>C: read yaml config\\n    M->>M: define result paths, create directories, record seeds\\n    M->>EX: run_monte_carlo(config) or run_empirical_analysis(config)\\n    EX->>S: simulate_paper_dgp(config, scenario, seed)\\n    EX->>E: estimate_parameters(data, config, method)\\n    EX->>EX: compute paper metrics with short formula comments\\n    EX-->>M: result tables and raw estimates\\n    M->>M: save csv/rds outputs to documented result directory\\n",
    "Anything UNCLEAR": "List any missing model equations, tuning constants, optimization constraints, simulation scenarios, or metric definitions that are not explicit in the paper."
}
[/CONTENT]

## Nodes: "<node>: <type>  # <instruction>"
- Implementation approach: <class 'str'>  # Summarize how to reproduce the statistical model, estimators, simulation, and metrics.
- File list: typing.List[str]  # Only need relative paths. ALWAYS include main.R. Prefer exactly config.yaml, simulation.R, estimators.R, experiments.R, main.R for statistics papers.
- Data structures and interfaces: typing.Optional[str]  # Use mermaid classDiagram code syntax. Include config, simulation, estimators, experiments, and main. Do not include model.R, metrics.R, or utils.R by default.
- Program call flow: typing.Optional[str]  # Use sequenceDiagram code syntax for model simulation, parameter estimation, metric computation, and result aggregation.
- Anything UNCLEAR: <class 'str'>  # Mention ambiguities in equations, estimators, DGP, assumptions, optimization, or metrics.

## Constraint
Format: output wrapped inside [CONTENT][/CONTENT] like the format example, nothing else.

## Action
Follow the instructions for the nodes, generate the output, and ensure it follows the format example."""
        }
    ]

    task_list_msg = [
        {
            "role": "user",
            "content": """Your goal is to break down the statistical reproduction into implementation tasks and dependency order.

The task list should implement only files needed for the paper's simulation DGP, parameter estimators, experiment workflow, metrics, and command-line entry point. Use concise R files. Do not include model.R, metrics.R, utils.R, trainer.py, dataset_loader.py, Python files, or neural-network training abstractions unless the paper explicitly requires them.

-----

## Format Example
[CONTENT]
{
    "Required packages": [
        "yaml",
        "stats"
    ],
    "Required Other language third-party packages": [
        "No third-party dependencies required"
    ],
    "Logic Analysis": [
        [
            "config.yaml",
            "Defines experiment settings, true parameter values, sample sizes, random seeds, estimator choices, optimization options, and output paths based only on the paper."
        ],
        [
            "simulation.R",
            "Generates synthetic data from the exact DGP/scenarios described in the paper, including model-specific random generation, sample sizes, noise distributions, dependence structures, and true parameters."
        ],
        [
            "estimators.R",
            "Implements every parameter estimation method described in the paper. Put parameter checks, safe log/probability calculations, objective functions, optimization routines, convergence diagnostics, and standard errors here when needed."
        ],
        [
            "experiments.R",
            "Defines paper metrics with short comments containing the metric names and formulas, records/uses replication seeds, runs Monte Carlo replications or empirical-data fitting workflows, calls simulation.R and estimators.R, aggregates results, and prepares table-ready outputs."
        ],
        [
            "main.R",
            "Entry point that loads configuration, starts with two numbered comment sections headed '### 1. Paper details requiring assumptions:' and '### 2. Output locations:', creates output directories, saves seed records, runs experiments, and saves csv/rds result tables."
        ]
    ],
    "Task list": [
        "config.yaml",
        "simulation.R",
        "estimators.R",
        "experiments.R",
        "main.R"
    ],
    "Full API spec": "",
    "Shared Knowledge": "All files share configuration values, true parameters, random seed handling, parameter naming conventions, estimator result schema, and metric definitions.",
    "Anything UNCLEAR": "List missing equations, estimator tuning constants, simulation settings, or metric definitions that must be inferred or approximated."
}
[/CONTENT]

## Nodes: "<node>: <type>  # <instruction>"
- Required packages: typing.Optional[typing.List[str]]  # Use minimal R packages appropriate for statistical computation, commonly yaml, stats, MASS, numDeriv, optimx, parallel, foreach, doParallel, ggplot2 only when needed.
- Required Other language third-party packages: typing.List[str]  # List system-level or non-R dependencies if any. If none, specify "No third-party dependencies required".
- Logic Analysis: typing.List[typing.List[str]]  # Provide file-level implementation details for simulation DGP, estimators, experiment metrics, optional empirical analysis, and main entry point.
- Task list: typing.List[str]  # Dependency order. Prefer exactly config.yaml, simulation.R, estimators.R, experiments.R, main.R.
- Full API spec: <class 'str'>  # Leave blank unless a service API is truly needed.
- Shared Knowledge: <class 'str'>  # Shared parameter schema, estimator result schema, random seed policy, result output paths, and metric definitions.
- Anything UNCLEAR: <class 'str'>  # Missing paper details or assumptions.

## Constraint
Format: output wrapped inside [CONTENT][/CONTENT] like the format example, nothing else.

## Action
Follow the node instructions above, generate your output accordingly, and ensure it follows the given format example."""
        }
    ]

    config_msg = [
        {
            "role": "user",
            "content": """You write elegant, modular, and maintainable configuration for an R-based statistics/econometrics reproduction.

Based on the paper, plan, and design specified previously, generate config.yaml for a statistics/econometrics reproduction.

Extract only details provided by the paper. Do not fabricate values. If the paper omits a value, set it to null and add a comment-like descriptive key explaining the uncertainty.

The configuration should cover, when present:
- model assumptions and true parameter values
- sample sizes and scenario grids
- number of Monte Carlo replications
- random seed policy
- estimator methods and tuning constants
- optimization settings and constraints
- simulation DGP settings
- reported metrics and output table names

You must write `config.yaml`.

ATTENTION: Use '##' to SPLIT SECTIONS, not '#'. Your output format must follow the example below exactly.

-----

# Format Example
## Code: config.yaml
```yaml
## config.yaml
experiment:
  n_replications: ...
  random_seed: ...
model:
  true_parameters: ...
simulation:
  sample_sizes: ...
estimators:
  methods: ...
metrics:
  reported: ...
outputs:
  results_dir: ...
...
```

-----

## Code: config.yaml
"""
        }
    ]

def api_call(msg, gpt_version):
    request = {"model": gpt_version, "messages": msg, **client.contract.request_options}
    return client.chat.completions.create(**request)

responses = []
trajectories = []
total_accumulated_cost = 0
task_manifest = None

for idx, instruction_msg in enumerate([plan_msg, file_list_msg, task_list_msg, config_msg]):
    current_stage = ""
    if idx == 0 :
        current_stage = f"[Planning] Overall plan"
    elif idx == 1:
        current_stage = f"[Planning] Architecture design"
    elif idx == 2:
        current_stage = f"[Planning] Logic design"
    elif idx == 3:
        current_stage = f"[Planning] Configuration file generation"
    print(current_stage)

    trajectories.extend(instruction_msg)

    completion = api_call(trajectories, gpt_version)
    
    # response
    completion_json = normalize_completion(completion, gpt_version)

    # print and logging
    print_response(completion_json)
    temp_total_accumulated_cost = print_log_cost(completion_json, gpt_version, current_stage, output_dir, total_accumulated_cost, provider_id)
    total_accumulated_cost = temp_total_accumulated_cost

    responses.append(completion_json)

    # trajectories
    message = get_completion_message(completion_json)
    trajectories.append({'role': message["role"], 'content': message["content"]})
    if idx == 2:
        task_manifest = parse_task_manifest_mapping(
            content_to_json(message["content"])
        )


# save
if task_manifest is None:
    raise RuntimeError("Planning did not produce a validated TaskManifest.")
save_task_manifest(output_dir, task_manifest)
save_accumulated_cost(f"{output_dir}/accumulated_cost.json", total_accumulated_cost)

safe_write_text(
    output_dir,
    validate_task_path("planning_response.json"),
    json.dumps(responses, ensure_ascii=False),
)
safe_write_text(
    output_dir,
    validate_task_path("planning_trajectories.json"),
    json.dumps(trajectories, ensure_ascii=False),
)
