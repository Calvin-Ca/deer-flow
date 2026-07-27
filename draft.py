from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage,ToolMessage


llm = ChatOpenAI(
    model= "qwen3-32b",
    api_key="sk-1e865ed7e785494db11138d0e905bed0",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    temperature=0,
    extra_body={"enable_thinking": False}
)

@tool
def calculator(expression:str) -> str:
    "计算数学表达式，例如 '2 + 3 * 4'"
    try:
        result = eval(expression)
        return str(result)
    except Exception as e:
        return f"计算出错：{e}"   

tools = [calculator]
tools_map = {"calculator":calculator}
llm_with_tools = llm.bind_tools(tools)

from typing import TypedDict, Annotated
import operator
class State(TypedDict):
    messages:Annotated[list,operator.add]

# 节点1
def llm_node(state:State):
    response = llm_with_tools.invoke(state["messages"])
    return {"messages":[response]}

def tool_node(state:State):
    last_message = state["messages"][-1]
    result = []
    for tool_call in last_message.tool_calls:
        tool_result = tools_map

