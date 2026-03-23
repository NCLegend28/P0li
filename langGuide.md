 LangGraph — Step-by-Step Guide                                                               
                                                                                                   
    ---
    1. Install

    uv add langgraph langchain-core

    ---
    2. Define Your State

    State is a typed dict that flows through the graph. Every node reads from and writes to it.

    from typing import TypedDict, Annotated
    import operator

    class AgentState(TypedDict):
        messages: Annotated[list[str], operator.add]  # append-only
        result: str
        iteration: int

    ---
    3. Write Node Functions

    Nodes are plain Python functions (sync or async) that receive state and return a partial
    update.

    def fetch_data(state: AgentState) -> dict:
        # do work
        return {"messages": ["Fetched data"], "result": "some data"}

    def process_data(state: AgentState) -> dict:
        result = state["result"].upper()
        return {"result": result, "messages": ["Processed"]}

    ---
    4. Build the Graph

    from langgraph.graph import StateGraph, END

    builder = StateGraph(AgentState)

    # Add nodes
    builder.add_node("fetch", fetch_data)
    builder.add_node("process", process_data)

    # Set entry point
    builder.set_entry_point("fetch")

    # Add edges (linear flow)
    builder.add_edge("fetch", "process")
    builder.add_edge("process", END)

    # Compile
    graph = builder.compile()

    ---
    5. Add Conditional Edges (Branching)

    def should_retry(state: AgentState) -> str:
        if state["iteration"] < 3 and not state["result"]:
            return "fetch"   # loop back
        return "process"     # move forward

    builder.add_conditional_edges(
        "fetch",
        should_retry,
        {
            "fetch": "fetch",       # maps return value → node name
            "process": "process",
        }
    )

    ---
    6. Invoke the Graph

    # Sync
    result = graph.invoke({"messages": [], "result": "", "iteration": 0})

    # Async
    result = await graph.ainvoke({"messages": [], "result": "", "iteration": 0})

    print(result["result"])

    ---
    7. Stream Output (Optional)

    async for chunk in graph.astream(initial_state):
        print(chunk)  # emits state after each node completes

    ---
    8. Add Persistence / Checkpointing (Optional)

    Lets the graph pause, resume, and support human-in-the-loop.

    from langgraph.checkpoint.memory import MemorySaver

    checkpointer = MemorySaver()
    graph = builder.compile(checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "session-123"}}
    result = graph.invoke(initial_state, config=config)

    # Resume same thread later
    result = graph.invoke(new_input, config=config)

    ---
    9. Human-in-the-Loop (Interrupt)

    from langgraph.graph import interrupt

    def review_node(state: AgentState) -> dict:
        approval = interrupt("Please review: " + state["result"])
        # execution pauses here until resumed
        return {"result": approval}

    Resume with:
    graph.invoke(Command(resume="approved"), config=config)

    ---
    Key Concepts Summary

    ┌────────────────────────┬─────────────────────────────────────────────┐
    │        Concept         │                 What It Is                  │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ StateGraph             │ The graph builder — holds nodes and edges   │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ State                  │ Typed dict passed between every node        │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ Node                   │ A function: (state) -> partial_state_update │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ Edge                   │ Fixed transition between two nodes          │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ Conditional Edge       │ Dynamic routing based on state              │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ END                    │ Terminal node — graph stops here            │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ Checkpointer           │ Persists state for resume/replay            │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ interrupt()            │ Pauses graph for human approval             │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ .compile()             │ Locks the graph and returns a runnable      │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ .invoke() / .ainvoke() │ Runs the graph to completion                │
    ├────────────────────────┼─────────────────────────────────────────────┤
    │ .astream()             │ Streams state chunks as nodes complete      │
    └────────────────────────┴─────────────────────────────────────────────┘

    ---
    Common Patterns

    Linear pipeline: A → B → C → END

    Loop with exit: fetch → decide → process → END (decide loops back to fetch conditionally)

    Parallel branches: Use Send API to fan out to multiple nodes simultaneously

    Agent loop: llm_call → tool_router → tool_executor → llm_call (loop until LLM decides to stop)