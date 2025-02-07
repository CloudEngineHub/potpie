import asyncio
import os
from contextlib import redirect_stdout
from typing import Any, AsyncGenerator, Dict, List

import agentops
import aiofiles
from crewai import Agent, Crew, Process, Task
from pydantic import BaseModel, Field

from app.modules.code_provider.code_provider_service import CodeProviderService
from app.modules.conversations.message.message_schema import NodeContext
from app.modules.intelligence.llm_provider.llm_provider_service import (
    LLMProviderService,
)
from app.modules.intelligence.prompts.prompt_service import PromptService
from app.modules.intelligence.prompts.prompt_schema import PromptType
from app.modules.intelligence.prompts_provider.agent_types import AgentLLMType
from app.modules.intelligence.tools.code_query_tools.get_code_file_structure import (
    get_code_file_structure_tool,
)
from app.modules.intelligence.tools.code_query_tools.get_node_neighbours_from_node_id_tool import (
    get_node_neighbours_from_node_id_tool,
)
from app.modules.intelligence.tools.kg_based_tools.ask_knowledge_graph_queries_tool import (
    get_ask_knowledge_graph_queries_tool,
)
from app.modules.intelligence.tools.kg_based_tools.get_code_from_multiple_node_ids_tool import (
    GetCodeFromMultipleNodeIdsTool,
    get_code_from_multiple_node_ids_tool,
)
from app.modules.intelligence.tools.kg_based_tools.get_code_from_node_id_tool import (
    get_code_from_node_id_tool,
)
from app.modules.intelligence.tools.kg_based_tools.get_code_from_probable_node_name_tool import (
    get_code_from_probable_node_name_tool,
)
from app.modules.intelligence.tools.kg_based_tools.get_nodes_from_tags_tool import (
    get_nodes_from_tags_tool,
)


class NodeResponse(BaseModel):
    node_name: str = Field(..., description="The node name of the response")
    docstring: str = Field(..., description="The docstring of the response")
    code: str = Field(..., description="The code of the response")


class RAGResponse(BaseModel):
    citations: List[str] = Field(
        ..., description="List of file names referenced in the response"
    )
    response: List[NodeResponse]


class DebugRAGAgent:
    def __init__(self, sql_db, llm, mini_llm, user_id):
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.max_iter = os.getenv("MAX_ITER", 5)
        self.sql_db = sql_db
        self.get_code_from_node_id = get_code_from_node_id_tool(sql_db, user_id)
        self.get_code_from_multiple_node_ids = get_code_from_multiple_node_ids_tool(
            sql_db, user_id
        )
        self.get_code_from_probable_node_name = get_code_from_probable_node_name_tool(
            sql_db, user_id
        )
        self.get_nodes_from_tags = get_nodes_from_tags_tool(sql_db, user_id)
        self.ask_knowledge_graph_queries = get_ask_knowledge_graph_queries_tool(
            sql_db, user_id
        )
        self.get_node_neighbours_from_node_id = get_node_neighbours_from_node_id_tool(
            sql_db
        )
        self.get_code_file_structure = get_code_file_structure_tool(sql_db)
        self.llm = llm
        self.mini_llm = mini_llm
        self.user_id = user_id
        self.prompt_service = PromptService(self.sql_db)

    async def create_agents(self):
        llm_provider_service = LLMProviderService.create(self.sql_db, self.user_id)
        preferred_llm, _ = await llm_provider_service.get_preferred_llm(self.user_id)
        agent_prompt = await self.prompt_service.get_prompts(
            "debug_rag_query_agent",
            [PromptType.SYSTEM],
            preferred_llm,
            max_iter=self.max_iter,
        )
        
        debug_rag_query_agent = Agent(
            role=agent_prompt["role"],
            goal=agent_prompt["goal"],
            backstory=agent_prompt["backstory"],
            tools=[
                self.get_nodes_from_tags,
                self.ask_knowledge_graph_queries,
                self.get_code_from_multiple_node_ids,
                self.get_code_from_probable_node_name,
                self.get_node_neighbours_from_node_id,
                self.get_code_file_structure,
            ],
            allow_delegation=False,
            verbose=True,
            llm=self.llm,
            max_iter=self.max_iter,
        )

        return debug_rag_query_agent

    async def create_tasks(
        self,
        query: str,
        project_id: str,
        chat_history: List,
        node_ids: List[NodeContext],
        file_structure: str,
        code_results: List[Dict[str, Any]],
        query_agent,
    ):
        if not node_ids:
            node_ids = []
        node_ids_list = [node.model_dump() for node in node_ids]

        llm_provider_service = LLMProviderService.create(self.sql_db, self.user_id)
        preferred_llm, _ = await llm_provider_service.get_preferred_llm(self.user_id)
        task_prompt = await self.prompt_service.get_prompts(
            "combined_task",
            [PromptType.SYSTEM],
            preferred_llm,
            max_iter=self.max_iter,
            chat_history=chat_history,
            query=query,
            project_id=project_id,
            node_ids=node_ids_list,
            file_structure=file_structure,
            code_results=code_results,
        )
        

        combined_task = Task(
            description=task_prompt,
            expected_output=(
                "Markdown formatted chat response to user's query grounded in provided code context and tool results"
            ),
            agent=query_agent,
        )

        return combined_task

    async def run(
        self,
        query: str,
        project_id: str,
        chat_history: List,
        node_ids: List[NodeContext],
        file_structure: str,
    ) -> AsyncGenerator[str, None]:
        agentops.init(
            os.getenv("AGENTOPS_API_KEY"), default_tags=["openai-gpt-notebook"]
        )
        code_results = []
        if len(node_ids) > 0:
            code_results = await GetCodeFromMultipleNodeIdsTool(
                self.sql_db, self.user_id
            ).run_multiple(project_id, [node.node_id for node in node_ids])
        debug_agent = await self.create_agents()
        debug_task = await self.create_tasks(
            query,
            project_id,
            chat_history,
            node_ids,
            file_structure,
            code_results,
            debug_agent,
        )

        read_fd, write_fd = os.pipe()

        async def kickoff():
            with os.fdopen(write_fd, "w", buffering=1) as write_file:
                with redirect_stdout(write_file):
                    crew = Crew(
                        agents=[debug_agent],
                        tasks=[debug_task],
                        process=Process.sequential,
                        verbose=True,
                    )
                    await crew.kickoff_async()

        agentops.end_session("Success")

        asyncio.create_task(kickoff())

        # Stream the output
        final_answer_streaming = False
        async with aiofiles.open(read_fd, mode="r") as read_file:
            async for line in read_file:
                if not line:
                    break
                if final_answer_streaming:
                    if line.endswith("\x1b[00m\n"):
                        yield line[:-6]
                    else:
                        yield line
                if "## Final Answer:" in line:
                    final_answer_streaming = True


async def kickoff_debug_rag_agent(
    query: str,
    project_id: str,
    chat_history: List,
    node_ids: List[NodeContext],
    sql_db,
    llm,
    mini_llm,
    user_id: str,
) -> AsyncGenerator[str, None]:
    provider_service = LLMProviderService(sql_db, user_id)
    crew_ai_llm = provider_service.get_large_llm(agent_type=AgentLLMType.CREWAI)
    crew_ai_mini_llm = provider_service.get_small_llm(agent_type=AgentLLMType.CREWAI)
    debug_agent = DebugRAGAgent(sql_db, crew_ai_llm, crew_ai_mini_llm, user_id)
    file_structure = await CodeProviderService(sql_db).get_project_structure_async(
        project_id
    )
    async for chunk in debug_agent.run(
        query, project_id, chat_history, node_ids, file_structure
    ):
        yield chunk
