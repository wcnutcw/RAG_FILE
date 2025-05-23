import logging
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import pandas as pd
import os
from tempfile import TemporaryDirectory
from langchain_experimental.agents import create_pandas_dataframe_agent
from langchain_openai import ChatOpenAI
from pymongo import MongoClient
import uuid
import pinecone
from pinecone import Pinecone
from dotenv import load_dotenv
from openai import AsyncOpenAI
from embed import parallel_upsert , batch_process_embedding_async
from cleansing import data_cleansing
import time
from retrival import *
from docx import Document
import fitz

#ENV
load_dotenv()

# กำหนดค่า logging
# logging.basicConfig(level=logging.DEBUG)
PINECONE_API_KEY :str
PINECONE_ENV : str
OPENAI_API_KEY : str
EMBEDDING : str
# MongoDB setup
mongo_client = MongoClient("mongodb://localhost:27017/")
db = mongo_client["file_agent_db"]
logs_collection = db["upload_logs"]

# Pinecone setup (ensure correct initialization)
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
PINECONE_ENV = os.getenv("PINECONE_ENV")
pc = Pinecone(api_key=PINECONE_API_KEY, environment= PINECONE_ENV)

# API 
app = FastAPI()

# OpenAI API key setup
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Embed model
embed = os.getenv("EMBEDDING")

#Post
agents = {}

@app.post("/upload")
async def upload_files(
    files: list[UploadFile] = File(...),
    index_name: str = Form(...),
    namespace: str = Form(...),
):
    try:
        start_time = time.perf_counter()

        if not files:
            # logging.error("No files uploaded")
            return JSONResponse(content={"error": "No files uploaded"}, status_code=400)

        # logging.debug("Processing files")
        temp_dir = TemporaryDirectory()
        dfs = []
        
        def read_docx(file_path):
            doc = Document(file_path)
            text = '\n'.join([para.text for para in doc.paragraphs])
            return text 
        def read_pdf(file_path):
            text = ""
            with fitz.open(file_path) as doc:
                for page in doc:
                    text += page.get_text()
            return text

        # Process each uploaded file
        for uploaded_file in files:
            file_path = os.path.join(temp_dir.name, uploaded_file.filename)
            with open(file_path, "wb") as f:
                f.write(await uploaded_file.read())

            # Read file based on type (CSV or Excel)
            if uploaded_file.filename.endswith(".csv"):
                df = pd.read_csv(file_path)
                dfs.append(df)
            elif uploaded_file.filename.endswith(".xlsx"):
                xls = pd.read_excel(file_path, sheet_name=None)
                for sheet_name, sheet_df in xls.items():
                    dfs.append(sheet_df)
            elif uploaded_file.filename.endswith(".docx"):
                text = read_docx(file_path)
                df = pd.DataFrame({"text":[text]})
            elif uploaded_file.filename.endswith(".pdf"):
                text = read_pdf(file_path)
                df = pd.DataFrame({"text":[text]})
                dfs.append(df)

        if not dfs:
            logging.error("No valid files found")
            return JSONResponse(content={"error": "No valid files found"}, status_code=400)

        df_con = pd.concat(dfs, ignore_index=True)
        df_combined = data_cleansing(df_con)
        # logging.debug(f"Combined dataframe shape: {df_combined.shape}")

        # Create the agent for dataframe interaction
        agent = create_pandas_dataframe_agent(
            ChatOpenAI(temperature=0, model="gpt-4"),
            df_combined,
            verbose=True,
            allow_dangerous_code=True
        )

        session_id = str(uuid.uuid4())
        agents[session_id] = agent

        # Checking if the index exists or create a new one if it doesn't
  
        try:
            index_list = pc.list_indexes().names()
            print(f"Existing indexes: {index_list}")
            logging.debug(f"Checking if index {index_name} exists")

            if index_name in index_list:
                print(f"Index '{index_name}' exists. Deleting it...")
                pc.delete_index(index_name)
                print(f"Index '{index_name}' deleted successfully.")
    
            logging.debug(f"Creating a new index {index_name}.")
            spec = pinecone.ServerlessSpec(cloud="aws", region=PINECONE_ENV)
            pc.create_index(
                name=index_name,
                dimension=1536,  # vector size
                metric="cosine",
                spec=spec
                )
            print(f"Index '{index_name}' has been created successfully.")
            
            # else:
            #     logging.debug(f"Index {index_name} already exists, proceeding with update.")

            # Now use the existing or newly created index for upsert
            index = pc.Index(index_name)
            vectors = []

            # Prepare vectors for upsert
            for i, row in df_combined.iterrows():
                metadata = row.to_dict()
                texts = [" ".join(map(str, row.to_dict().values())) for _, row in df_combined.iterrows()]
                try:
                    embedding = await batch_process_embedding_async(texts ,model=embed)
                except ValueError as e:
                    logging.error(f"Error generating embedding: {e}")
                    return JSONResponse(content={"error": str(e)}, status_code=500)

                # Add the vector data for upsert
                vectors.append({
                    "id": f"vec-{i}",
                    "values": embedding[0],  # เอาอันแรก เพราะ batch มีแค่ 1 ข้อความ
                    "metadata": metadata
                })

            # Perform upsert operation to insert or update data in the index
            try:
                    await parallel_upsert(index, vectors, namespace, batch_size=100)
                    logging.debug(f"Successfully upserted vectors into the index {index_name}")
                    end_time = time.perf_counter()
                    processing_time = end_time - start_time
                    print(f"{processing_time} second")
            except Exception as e:
                    logging.error(f"Error upserting vectors to Pinecone: {e}")
                    return JSONResponse(content={"error": "Failed to upsert vectors to Pinecone"}, status_code=500)

        except Exception as e:
            logging.error(f"Error while checking or creating Pinecone index: {e}")
            return JSONResponse(content={"error": "Failed to check or create Pinecone index"}, status_code=500)



        agents[session_id] = {
                "agent": agent,
                "index_name": index_name,
                "namespace": namespace
            }
        # Log upload details to MongoDB
        log = {
            "session_id": session_id,
            "files": [f.filename for f in files],
            "index_name": index_name,
            "namespace": namespace,
            "timestamp": pd.Timestamp.now().isoformat()
        }
        logs_collection.insert_one(log)

        return {"session_id": session_id}

    except Exception as e:
        logging.error(f"Error in /upload endpoint: {str(e)}")
        return JSONResponse(content={"error": f"Internal server error: {str(e)}"}, status_code=500)

@app.post("/query")
async def query(session_id: str, question: str):
    try:
        # ดึง index_name กับ namespace จาก MongoDB
        log = logs_collection.find_one({"session_id": session_id})
        if not log:
            logging.error(f"No matching log found for session_id: {session_id}")
            return JSONResponse(content={"error": "No matching log found"}, status_code=404)

        index_name = log["index_name"]
        namespace = log["namespace"]

        # 1. ดึง context จาก Pinecone
        context = await retrieve_context_from_pinecone(question, index_name, namespace)

        # 2. สร้าง prompt สำหรับ GPT
        prompt = f"""
        บริบทต่อไปนี้คือข้อมูลจากตาราง: {context}
        คำถามของฉันคือ: "{question}"

        กรุณาตอบเป็นภาษาไทยอย่างละเอียด"""

        # 3. เรียก GPT เพื่อสร้างคำตอบ
        llm = ChatOpenAI(temperature=0, model="gpt-4")
        response = await llm.ainvoke(prompt)

        return {"response": response.content}

    except Exception as e:
        logging.error(f"Error processing query: {e}")
        return JSONResponse(content={"error": "Error processing query"}, status_code=500)
