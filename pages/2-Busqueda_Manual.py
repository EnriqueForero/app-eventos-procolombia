
import streamlit as st
import os, toml, requests
import requests
import datetime as dt
import pandas as pd
import nltk
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
from bs4 import BeautifulSoup
from typing import List, Dict, Optional, Union
from langchain.utilities import TextRequestsWrapper
from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain.output_parsers import PydanticOutputParser, YamlOutputParser
from langchain_core.pydantic_v1 import BaseModel, Field
# from langchain.document_loaders import WebBaseLoader
from langchain_community.document_loaders import WebBaseLoader
from langchain.schema.prompt_template import format_document
from langchain_google_genai import ChatGoogleGenerativeAI
import google.generativeai as genai
from menu import menu

from pages.lib.funciones import extraer_informacion_general_gemini, filtrar_df, cargar_eventos_procesados_archivo, cargar_configuracion, cargar_contraseñas, obtener_criterios_busqueda
from pages.lib.funciones import limpiar_df_event, web_scrapper
from pages.lib.funciones_snowflake import sf_cargar_eventos_procesados_db, sf_check_event_db, sf_insert_rows
from pages.lib.funciones_mongo import mdb_cargar_eventos_procesados_db, mdb_check_event_db, mdb_insert_doc

# Definicion de rutas y constantes
PATH_CWD = os.getcwd()
PATH_DATA = PATH_CWD + "/src/data/"
PATH_IMG  = PATH_DATA + 'img/'
FN_KEYW = 'db_eventos_keyw.csv'
FN_EVENTS = 'events_data.xlsx'
FN_KEYW_JSON = 'app_config.json'
ACCESS_PATH = PATH_CWD + "/.scrts/access.toml"
#
MODELS_DICT = {'Gemini':0, 'GROG-LLAMA2':1}
st.set_page_config(layout="wide")
menu()
st.image(PATH_IMG + "header_cocora.jpg")
st.subheader("Busqueda Manual de informacion por URL")


    
class Event(BaseModel):
    title: str  = Field(description="The name of the event, dont use initials, dont use punctuation marks")
    year: Optional[str]   = Field(description="The year of the event")
    country: Optional[str] = Field(description="The location of the event")

class json_resp_events(BaseModel):
    events: List[Event] = Field(..., description="The Event details")

class eventAsist(BaseModel):
    title: str  = Field(description="The name of the event, dont use initials, dont use punctuation marks")
    participants: Optional[str]   = Field(description="The resume of the information in few words about event participation, if not information or you are not sure put None")


def query_google_search(google_query, page, search_engine_keys, add_params = {}):
  """
  Query the Google Custom Search API and return the results in a dictionary.

  Args:
      google_query (str): The query to search for.
      page (int): The page number to retrieve.
  Returns:
      A dictionary containing the search results
  """

  # using the first page
  page = page
  start = (page - 1) * 10 + 1

#   url = f"https://www.googleapis.com/customsearch/v1?key={search_engine_keys['KEY']}&cx={search_engine_keys['ID']}&q={google_query}&start={start}" + add_args
  url = "https://www.googleapis.com/customsearch/v1"
  params = {
      'q' : google_query,
      'key' : search_engine_keys['KEY'],
      'cx' : search_engine_keys['ID']
  }
  params.update(add_params)
  print(url)
  print(params)
  try:
      # Make the GET request to the Google Custom Search API
      google_response = requests.get(url, params=params)

      # Check if the request was successful (status code 200)
      if google_response.status_code == 200:
          # Parse the JSON response
          google_response_data = google_response.json()
          google_response_items = {}
          # get the result items
          search_items = google_response_data.get("items")
          # iterate over 10 results found
          for i, search_item in enumerate(search_items, start=1):
              try:
                  long_description = search_item["pagemap"]["metatags"][0]["og:description"]
              except KeyError:
                  long_description = "N/A"
              # get the page title
              title = search_item.get("title")
              # page snippet
              snippet = search_item.get("snippet")
              # alternatively, you can get the HTML snippet (bolded keywords)
              html_snippet = search_item.get("htmlSnippet")
              # extract the page url
              link = search_item.get("link")
              google_response_items[i] = {
                  'title': title,
                  'snippet': snippet,
                  'long_description': long_description,
                  'link': link
              }
          return google_response_items

      else:
          print(f"Error: {google_response.status_code}")
          return None
  except Exception as e:
      print(f"An error occurred: {e}")
      return None

def es_archivo_pdf(url):
    try:
        # Realizar una solicitud HEAD para obtener solo los encabezados de la respuesta
        response = requests.head(url)
        
        # Verificar si la respuesta tiene el tipo de contenido "application/pdf"
        if 'application/pdf' in response.headers.get('Content-Type', ''):
            return True
        else:
            return False
    except requests.exceptions.RequestException as e:
        print("Error al hacer la solicitud:", e)
        return False

def preprocess(sentence):
    try:
        nltk.data.find('tokenizers/punkt')
        nltk.data.find('corpora/stopwords')
    except LookupError:
        nltk.download('punkt')
        nltk.download('stopwords')
    stop_words = list(set(stopwords.words('english'))) + list(set(stopwords.words('spanish')))
    word_tokens = word_tokenize(sentence.lower())
    return [word for word in word_tokens if word.isalnum() and word not in stop_words ]

def jaccard_similarity(sentence1, sentence2):
    words1 = set(preprocess(sentence1))
    words2 = set(preprocess(sentence2))
    intersection = len(words1.intersection(words2))
    try:
        if len(words1) == len(intersection):
            return 1
        else:
            union = len(words1.union(words2))
            return intersection / union if union != 0 else 0 
    except:
        union = len(words1.union(words2))
        return intersection / union if union != 0 else 0 

def check_similar (new_key, old_keys):
    for old_key in old_keys:
        similarity_score = jaccard_similarity(new_key, old_key)
        if similarity_score >= 0.7:
            return True
        else:
            continue
    return False

def extraer_informacion_eventos_rel_gemini(url, event, API_KEY_GEMINI):
    
    os.environ["GOOGLE_API_KEY"] = API_KEY_GEMINI
    llm = ChatGoogleGenerativeAI(model="gemini-pro")
    model = genai.GenerativeModel('gemini-pro')
    llm_prompt_template = """Your Task is to extract any event showed in following "Context" that can be related to the "event information". 
    "event information":{event_str}
    "context":{context_str}
    \n{format_instructions}\n
    """
    parser = YamlOutputParser(pydantic_object=json_resp_events)

    # To extract data from WebBaseLoader
    doc_prompt = PromptTemplate.from_template("{page_content}")
    
    # Realizar el query a Gemini
    llm_prompt = PromptTemplate.from_template(llm_prompt_template)

    llm_prompt = PromptTemplate(
        template=llm_prompt_template,
        input_variables=["context_str", "event_str"],
        partial_variables={"format_instructions": parser.get_format_instructions()},
    )
    context = web_scrapper(url)
    if context.startswith('Not Acceptable!'):
        loader = WebBaseLoader(url)
        docs = loader.load()
        doc_prompt = PromptTemplate.from_template("{page_content}")
        context = "\n\n".join(format_document(doc, doc_prompt) for doc in docs)
        
    tokens_size = int(model.count_tokens(str(llm_prompt) + context).total_tokens)
    if tokens_size > 30000:
        return None
    else:
        stuff_chain = llm_prompt | llm | parser
        llm_result = stuff_chain.invoke({"context_str": context, "event_str": event} )
        return llm_result

def rel_events_parser(yaml_events, df_hist_rel_events, event_key):
    df_rel_events = pd.DataFrame(columns=['event_key', 'rel_event_link', 'rel_event_key','rel_event_title', 'rel_event_year', 'rel_event_country'])

    for event in yaml_events.events:
        events_related_parsed = {}
        rel_event_key = event.title + " | " + event.country + " | " + str(event.year)
        print(check_similar(event_key, [rel_event_key]) , check_similar(rel_event_key, df_hist_rel_events['rel_event_key']))
        if not check_similar(event_key, [rel_event_key]) and not check_similar(rel_event_key, df_hist_rel_events['rel_event_key']):
            if int(event.year) > dt.datetime.today().year -20:
                events_related_parsed['event_key'] = event_key
                events_related_parsed['rel_event_key'] = event.title + " | " + event.country + " | " + str(event.year)
                events_related_parsed['rel_event_title'] = event.title
                events_related_parsed['rel_event_country'] = event.country
                events_related_parsed['rel_event_year'] = event.year
                df_events_related_parsed = pd.DataFrame(events_related_parsed, index=[0])
                df_rel_events = pd.concat([df_rel_events, df_events_related_parsed])
    return df_rel_events

def buscar_eventos_relacionados(llm_result_event, contraseñas):

    df_rel_events = pd.DataFrame(columns=['event_key',  'rel_event_key','rel_event_title', 'rel_event_year', 'rel_event_country','rel_event_link'])
    if llm_result_event.there_is_event == "True":

        for i in range(3):
            print(i)
            
            if i == 0:
                add_args = {
                    'lr': 'lang_eng|lang_esp'
                }
            elif i == 1:
                add_args = {
                    'lr': 'lang_esp'
                }
            elif i == 2:
                add_args = {
                    'lr': 'lang_esp',
                    'cr': 'countryCO'
                }
            if llm_result_event.title !="" and llm_result_event.general_title !=None:
                link_or_name = llm_result_event.general_title
                search_pattern = f"related: {link_or_name} "
            else:
                link_or_name = llm_result_event.title
                search_pattern = f"related: {link_or_name} "
            print("Criterio Busqueda:{}".format(search_pattern))
            google_query_result = query_google_search(search_pattern, 1, contraseñas["api_google_search"],add_args)
            for url in google_query_result:
                if es_archivo_pdf(google_query_result[url]['link']):
                    continue
                else:
                    print(google_query_result[url]['link'], search_pattern)
                    ref_event_info = "title:" + llm_result_event.title + "|" +"resume:" + llm_result_event.description + "|"+"country:" + llm_result_event.country  + "|"+"year:" + llm_result_event.year
                    ref_event_key = llm_result_event.title + " | " + llm_result_event.country + " | " + llm_result_event.year 
                    try:   
                        yaml_events_related = extraer_informacion_eventos_rel_gemini(google_query_result[url]['link'], ref_event_info , contraseñas["api_gemini"]['KEY'])
                        df_events_related_link = rel_events_parser(yaml_events_related, df_rel_events, ref_event_key)
                        df_events_related_link ['rel_event_link'] = google_query_result[url]['link']
                        df_rel_events = pd.concat([df_rel_events, df_events_related_link])
                        if len(df_rel_events) >= 5:
                            return df_rel_events
                    except Exception as e:
                        print(e)
                        continue
    return df_rel_events
    
def extraer_informacion_asistentes_gemini(url, event, API_KEY_GEMINI):
    
    os.environ["GOOGLE_API_KEY"] = API_KEY_GEMINI
    llm = ChatGoogleGenerativeAI(model="gemini-pro")
    model = genai.GenerativeModel('gemini-pro')
    llm_prompt_template = """Tu tarea es extraer de "context" la informacion disponible del numero de asistentes al evento {event_str} en el idioma del contexto". 
    "context":{context_str}
    \n{format_instructions}\n
    """
    parser = YamlOutputParser(pydantic_object=eventAsist)

    # To extract data from WebBaseLoader
    doc_prompt = PromptTemplate.from_template("{page_content}")
    
    # Realizar el query a Gemini
    llm_prompt = PromptTemplate.from_template(llm_prompt_template)

    llm_prompt = PromptTemplate(
        template=llm_prompt_template,
        input_variables=["context_str", "event_str"],
        partial_variables={"format_instructions": parser.get_format_instructions()},
    )
    context = web_scrapper(url)
    if context.startswith('Not Acceptable!'):
        loader = WebBaseLoader(url)
        docs = loader.load()
        doc_prompt = PromptTemplate.from_template("{page_content}")
        context = "\n\n".join(format_document(doc, doc_prompt) for doc in docs)
    tokens_size = int(model.count_tokens(str(llm_prompt) + context).total_tokens)
    if tokens_size > 30000:
        return None
    else:
        stuff_chain = llm_prompt | llm | parser
        llm_result = stuff_chain.invoke({"context_str": context, "event_str": event} )
        return llm_result
    
def buscar_informacion_asistentes(llm_result_event, contraseñas):
    asistants_list = []
    for i in range(3):
        print(i)
        
        if llm_result_event.place !=None and llm_result_event.place !="":
            location = llm_result_event.place
        elif llm_result_event.country !=None and llm_result_event.country !="":
            location = llm_result_event.country
        else:
            location = ""
        search_pattern = f"{llm_result_event.general_title} {location}"
        if i == 0:
            add_args = {
                'lr': 'lang_eng|lang_esp'
            }
        elif i == 1:
            add_args = {
                'lr': 'lang_esp'
            }
        elif i == 2:
            add_args = {
                'lr': 'lang_esp',
                'cr': 'countryCO'
            }
        google_query_result = query_google_search(search_pattern, 1, contraseñas["api_google_search"], add_args)
  
        for url in google_query_result:
            if es_archivo_pdf(google_query_result[url]['link']):
                continue
            else:
                print(google_query_result[url]['link'], search_pattern)
                ref_event_info = "title:" + llm_result_event.title + "|" +"resume:" + llm_result_event.description + "|"+"country:" + llm_result_event.country  + "|"+"year:" + llm_result_event.year
                try:   
                    yaml_envent_asistants = extraer_informacion_asistentes_gemini(google_query_result[url]['link'], ref_event_info , contraseñas["api_gemini"]['KEY'])
                    if yaml_envent_asistants.participants not in [None, 'None', '', ' '] and not check_similar(yaml_envent_asistants.participants, asistants_list):
                        asistants_list.append(yaml_envent_asistants.participants)
                        print(asistants_list)
                        if len(asistants_list) >=3:
                            return "|".join(asistants_list)  
                except Exception as e:
                    print(e)
                    continue
    return "|".join(asistants_list) 

def buscar_evento(url, contraseñas):
    date =  dt.datetime.today().date().strftime("%Y-%m-%d")
    sel_db_mongo = True 
    sel_db_snowflake = False
    llm_result = extraer_informacion_general_gemini(url, contraseñas["api_gemini"]['KEY'])
        
    if llm_result != None:
        if (sel_db_snowflake and sf_check_event_db(url, '', contraseñas['snowflake'])) or \
        (sel_db_mongo and mdb_check_event_db(url, '', contraseñas['mongo_db'])):
            st.write("Evento Previamente procesado")
            return llm_result
        else:
            df_event_info = pd.DataFrame([llm_result.__dict__])
            df_event_info ['status']  = 'OK'   
            df_event_info['google_title'] = None
            df_event_info['google_snippet'] = None
            df_event_info['google_long_description'] = None
            df_event_info['google_url'] = url
            df_event_info['search_criteria'] =  None
            df_event_info['date_processed'] =  date
            df_event_info = limpiar_df_event(df_event_info)
            if sel_db_snowflake:
                resultado = sf_insert_rows(df_event_info, 'fct_eventos', contraseñas['snowflake'])
            elif sel_db_mongo:
                df_event_info['date_processed'] = pd.to_datetime(df_event_info['date_processed'])
                resultado = mdb_insert_doc(df_event_info, 'fct_eventos', contraseñas['mongo_db'])
            if resultado == True:
                st.write("Evento Insertados Correctamente")
            else:
                st.write("Error Insertando Evento. Error: {}".format(resultado))
            return llm_result
    else:
        return None
    
def main():
    import time
    df_rel_events = pd.DataFrame(columns=['event_key',  'rel_event_key','rel_event_title', 'rel_event_year', 'rel_event_country','rel_event_link'])
    contraseñas = cargar_contraseñas(ACCESS_PATH)

    st.divider()
    col1, col2 = st.columns([2, 5])
    col1.text_input("Ingrese la url", key="url")
    evento_rel = col1.checkbox('Buscar eventos relacionados', key="evento_rel")
    evento_asistente = col1.checkbox('Buscar asistentes al evento', key="evento_asistente")
    col1.divider()
    iniciar = col1.button("Iniciar Busqueda")
    
    # Añadir un botón a la interfaz de usuario
    if iniciar:
        placeholder_1 = col2.empty()
        placeholder_1.write(f"⏳ Buscando Informacion de eventos en la pagina {st.session_state.url}!!")
        llm_result = buscar_evento(st.session_state.url, contraseñas)
        if llm_result != None:
        
            if llm_result.there_is_event == "True":
                placeholder_1.write(f"✔️ Hemos encontrado eventos en la pagina {st.session_state.url}")

                c_1 = col2.container(border=True)
                with col2.expander(f"Ver detalles del Evento: **{llm_result.title}, {llm_result.country}, {llm_result.year}**"):
                    event_info = f"""**- Titulo del evento:** {llm_result.title}  
                    **- Pais del evento:** {llm_result.country} 
                    **- Año del evento:** {llm_result.year} 
                    **- Fecha del evento:** {llm_result.date}  
                    **- Detalles:** {llm_result.description}
                    """
                    st.markdown(event_info)
                
                if evento_rel:
                    placeholder_2 = col2.empty()
                    placeholder_2.write(f"⏳ Buscando Informacion de eventos relacionados a {llm_result.title}!!")

                    df_rel_events = buscar_eventos_relacionados(llm_result, contraseñas)
                    if len(df_rel_events) > 0:
                        placeholder_2.write(f"✔️ Hemos encontrado eventos relacionados a  {llm_result.title}")
                        
                        with col2.expander(f"Ver detalles de los eventos relacionados"):
                            st.dataframe(df_rel_events, use_container_width=True, hide_index  = True)

                if evento_asistente:
                    placeholder_3 = col2.empty()
                    placeholder_3.write(f"⏳ Buscando Informacion de asistentes al evento a {llm_result.title}!!")
                    asistentes = buscar_informacion_asistentes(llm_result, contraseñas)
                    if asistentes:
                        placeholder_3.write(f"✔️ Hemos informacion de asistentes al evento a {llm_result.title}!!")
                        
                        with col2.expander(f"Ver Informacion de los asistentes"):
                            st.write(asistentes)
                            
        else:
            placeholder_1.write(f"⚠️ No hemos encontrado eventos en la pagina {st.session_state.url}")
            


            
            
            
            
if __name__ == "__main__":
    main()