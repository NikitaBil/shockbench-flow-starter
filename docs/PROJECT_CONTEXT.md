\# ShockBench-Flow Team Context



\## Goal



We are participating in the ShockBench-Flow hackathon.



Our goal is to build a self-contained offline Agent that minimizes

the total economic cost of operating the logistics network under uncertainty.



The agent must make weekly decisions about flows of goods through routes.



\## Mental model



Each week:



Observation

→ analyse inventory

→ analyse demand forecast

→ analyse early warnings

→ estimate future shortages

→ evaluate routes

→ allocate flows

→ return Action



\## Agent interface



The submission defines:



class Agent:

&#x20;   def \_\_init\_\_(self, config):

&#x20;       ...



&#x20;   def act(self, observation):

&#x20;       ...

&#x20;       return {"flows": flows}



\_\_init\_\_ runs once per episode.

act() runs once per simulated week.



\## Important terms



Observation:

current information available to the agent.



Stock / inventory:

goods currently stored at nodes.



Backlog:

demand that has not been satisfied.



Demand forecast:

expected demand for the next 8 weeks.



Route:

a possible logistics route.



Routing:

choosing which routes should carry goods.



Flow:

quantity sent through an action slot / route this week.



Capacity:

maximum usable capacity of a route.



Lead time:

time required for a shipment to arrive.



Early Warning:

a noisy signal indicating that a disruption may happen.

It is not certainty.



Scenario:

one generated history of disruptions.



Episode:

one complete simulation.



RSS:

Resilience Skill Score.

0 = naive reference.

1 = clairvoyant reference.

Higher is better.



\## Important constraints



The submitted agent must be self-contained and work offline.



No external APIs.

No network access.

No LLM calls at runtime.



Allowed submission imports:

\- Python standard library

\- NumPy

\- SciPy

\- PyTorch CPU



The algorithm must fit within the competition CPU limits.



Do not hard-code Small network dimensions.

Read shapes from config.



\## Current strategy idea



Working concept:



Early-Warning Adaptive Predictive Planner



The agent should:



1\. inspect current stock, backlog and shipments;

2\. estimate future inventory using the 8-week demand forecast;

3\. detect likely shortages before they occur;

4\. interpret Early Warnings as uncertainty rather than certainty;

5\. increase or decrease safety buffers depending on risk;

6\. evaluate routes using cost, lead time, tariffs, capacity and risk;

7\. allocate flows economically;

8\. avoid excessive inventory and unnecessary rerouting.



The central question each week is:



"Given what is known now, which shipments minimize expected total

economic cost under uncertainty?"



\## Development principles



Do not assume a more complicated algorithm is better.



Every meaningful change must be evaluated experimentally.



Use:



sbf evaluate <agent> --task=small



and especially:



sbf compare <new\_agent> <old\_agent> --task=small



Compare versions on the same scenarios.



Use custom entropy roots for development and tuning.

Do not overfit the public 20-episode development split.



Before submission run:



sbf check <agent> --task=small



\## Current priorities



Before implementing an advanced policy:



1\. understand template agent;

2\. understand heuristic agent;

3\. inspect docs/fields;

4\. understand all observation fields;

5\. establish a reproducible baseline;

6\. implement one improvement at a time;

7\. measure every improvement.

