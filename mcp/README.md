# KiCad MCP

Servidor MCP (Model Context Protocol) para o Claude montar esquemáticos e placas no KiCad.
Não faz parte do KiCad oficial; existe só no fork `maxrosan/kicad-source-mirror`.

## Como funciona

- Por padrão o servidor sobe um `kicad-cli api-server` (o KiCad sem janela) com um socket
  próprio. No **modo ao vivo** (`KICAD_API_SOCKET`) ele usa o KiCad aberto na tela.
- O cliente Python é gerado dos `.proto` deste repositório (`api/proto`), então sempre bate
  com o KiCad compilado deste checkout. A biblioteca `kipy` não é usada.
- Ligações no esquemático são labels de rede no ponto de conexão de cada pino, não fios:
  pinos com o mesmo nome de rede ficam ligados.
- ERC, DRC, netlist, imagens, STEP e Specctra DSN usam o `kicad-cli`.

## Ferramentas

Esquemático

| Ferramenta | O que faz |
|---|---|
| `kicad_status` | versão, modo (headless/ao vivo) e esquemático aberto |
| `new_schematic` / `open_schematic` | cria (com projeto) ou abre um `.kicad_sch`; ao vivo, usa o que está aberto |
| `search_symbols` / `search_footprints` | busca nas bibliotecas por nome, descrição e palavras-chave |
| `symbol_pins` | pinos, footprint padrão e filtros de um símbolo, sem colocá-lo |
| `place_symbol` / `update_symbol` / `remove_symbol` / `list_symbols` | peças |
| `connect_pins` / `mark_no_connect` | ligações; recusa ligar um pino já em outra rede |
| `save_schematic`, `run_erc`, `get_netlist`, `export_svg` | salvar e verificar |

Revisão e firmware

| Ferramenta | O que faz |
|---|---|
| `review_design` | o que o ERC não pega: footprint faltando, rede de um pino só, desacoplamento, capacitores dos reguladores, tensão dos capacitores, e regras do ESP32 (pinos de strap, ADC2 com Wi-Fi, GPIO34-39 só entrada, pinos da flash, rede de reset do EN); devolve também reguladores, divisores e pull-ups |
| `get_pin_map` / `write_pin_header` | `pins.h` (Arduino ou ESP-IDF) com um `#define` por GPIO com o nome da rede e o fator dos divisores |

Placa

| Ferramenta | O que faz |
|---|---|
| `update_board_from_schematic` | cria a placa e traz footprints e redes (como "Atualizar PCB a partir do esquemático") |
| `auto_place` | posicionamento inicial: peças pequenas junto do CI com que se ligam, grupos em linhas |
| `move_footprints` / `list_footprints` | mover, girar, virar de lado |
| `set_board_outline` | contorno retangular, ou ajustado em volta das peças |
| `set_power_netclass` | classe de rede com trilhas largas para bateria, motor e reguladores |
| `add_ground_zone` | plano de GND nas duas faces |
| `set_fab_rules` | mínimos de trilha, isolação, furo e borda do fabricante (preset `jlcpcb`, 2 camadas) |
| `autoroute` | roteamento com o Freerouting; os keepouts dos footprints (antena do ESP32) viram keepouts da placa durante o roteamento |
| `run_drc` | DRC com conexões faltando e paridade com o esquemático |
| `export_board_view` / `export_step` | PDF 2D ou render 3D para revisar; STEP para a parte mecânica |

## Botão "Perguntar ao Claude" no KiCad

`install_kicad_plugin.py` instala um plugin de API (runtime `exec`) em
`Documentos\KiCad\10.99\plugins\com.maxrosan.askclaude`. Ele põe um botão nos editores de
esquemático e de placa que lê a seleção atual, pergunta o que você quer saber e roda o Claude
Code (`claude -p`) com este MCP ligado ao KiCad aberto. Por padrão o Claude só pode ler o
projeto; marcando "Permitir que o Claude altere o projeto" ele pode editar.

```bat
C:\Users\mr650\miniconda3\envs\kicad\python.exe mcp\install_kicad_plugin.py
```

Requisitos: a API ligada no KiCad (Preferences > Plugins > Enable KiCad API) e o Claude Code
instalado (`claude` no PATH). Depois de instalar, reinicie o KiCad ou use Reload em
Preferences > Plugins.

## Instalação

Tudo roda no ambiente conda `kicad` (ver o CLAUDE.md na raiz), que precisa de `pynng` e
`mcp` (SDK 2.x):

```bat
call C:\Users\mr650\miniconda3\condabin\conda.bat activate kicad
python mcp\gen_proto.py
```

Repita o `gen_proto.py` depois de trazer atualizações do KiCad que mexam em `api/proto`.
O servidor está registrado para este projeto no `.mcp.json` da raiz do repositório.

## Configuração

- `KICAD_CLI`: caminho do `kicad-cli.exe` (padrão: o da instalação deste build).
- `KICAD_SHARE`: pasta `share/kicad` com `symbols/` e `footprints/`.
- `KICAD_MCP_MODE`: `auto` (padrão), `live` ou `headless`. No `auto`, se houver um KiCad aberto
  com a API ligada o MCP trabalha nele (modo ao vivo) e não sobe um segundo KiCad; senão sobe o
  servidor headless. Dois KiCads no mesmo projeto salvam um por cima do outro (é o aviso
  "Project ... is already open"), por isso o modo headless também se recusa a abrir um projeto
  que tem arquivo de trava (`~<projeto>.kicad_pro.lck`). Para criar projeto novo com o KiCad
  aberto, use `headless`.
- `KICAD_API_SOCKET`: força o modo ao vivo num socket específico.
- `FREEROUTING_JAR`: caminho do `.jar` do Freerouting, para o `autoroute`. Instalado em
  `build\tools\freerouting-2.5.0.jar` (release oficial do GitHub, SHA-256 conferido) e já
  configurado no `.mcp.json`.
- `JAVA`: o Java usado pelo Freerouting. O 2.5 exige Java 25; por padrão o MCP usa o OpenJDK 25
  do ambiente conda `kicad` (o Java 17 do PATH não serve).

## Correções no KiCad que o MCP usa

Feitas neste fork e candidatas a Merge Request no KiCad oficial:

- `SearchLibraries` não tinha implementação (`common/api/api_handler_libraries.cpp`).
- `GetItemsFromLibrary` devolvia símbolos derivados (`extends`) sem pinos
  (`eeschema/api/api_handler_sch_libraries.cpp`).
- Zonas de footprint (ex.: o keepout da antena do ESP32) saíam na API em coordenadas da
  biblioteca, enquanto pads e linhas saem em coordenadas da placa; mover um footprint pela API
  deixava a zona para trás (`pcbnew/zone.cpp`).

## Limitações conhecidas

- A API serializa os itens de um footprint em coordenadas da placa, então mover um footprint
  exige transformar todos eles; o MCP faz isso (`pcb.move_footprint_instance`).
- Mover um footprint pela API ainda perde detalhes cosméticos (o bloco `units`, o ângulo de
  textos `${REFERENCE}` na camada Fab), e o DRC passa a avisar "footprint does not match
  library" para esse footprint.
- O Freerouting pode terminar com algumas conexões sem rota (na placa da garra ficaram 2 de 57)
  e o plano de GND pode ficar em ilhas; o `run_drc` mostra o que falta para terminar à mão.
- O footprint do ESP32-WROOM-32E tem vias térmicas de 0,2 mm; o preset `jlcpcb` mantém o furo
  mínimo de 0,3 mm de 2 camadas, então o DRC as aponta.
- O posicionamento automático é um ponto de partida: antena do ESP32 na borda, conectores nas
  bordas e o laço da fonte chaveada ainda precisam de ajuste.
