# CLAUDE.md

Fork local do código-fonte do KiCad (C++/CMake). As regras de código do projeto
(estilo, comentários, arquivos gerados, política de conteúdo gerado por ferramenta)
estão em AGENTS.md:

@AGENTS.md

## Repositório e atualizações do KiCad

- `origin`: o fork no GitHub, https://github.com/maxrosan/kicad-source-mirror.
- `upstream`: o KiCad oficial no GitLab (push desabilitado).
- `master` fica igual ao upstream; o trabalho vai no branch `max/main`.

To bring in KiCad updates:

```
git fetch upstream
```

```
git merge upstream/master
```

## Ambiente conda `kicad`

Quando precisar de uma ferramenta que não está instalada (Python, scripts auxiliares,
utilitários de linha de comando etc.), instale e rode dentro do ambiente conda
`kicad`. Não instale nada no ambiente `base` nem no Python do sistema.

- O conda fica em `C:\Users\mr650\miniconda3` e não está no PATH do PowerShell.
  Chame-o pelo caminho completo, por exemplo:
  `& "C:\Users\mr650\miniconda3\Scripts\conda.exe" run -n kicad <comando>`
- O ambiente usa só o canal conda-forge. Instalar pacotes:
  `conda install -n kicad --override-channels -c conda-forge <pacote>`.
- O ambiente já tem as ferramentas e bibliotecas de build do KiCad, todas
  compiladas com MSVC: cmake, ninja, swig, gettext, python 3.12, protobuf, boost,
  wxWidgets 3.3 (com webview), OCCT 7.9, ngspice, nng, glm, libgit2, cairo,
  harfbuzz, icu, curl, sqlite, zstd, vulkan-headers, glslang e spirv-tools.
  Os arquivos ficam em `C:\Users\mr650\miniconda3\envs\kicad\Library`; use esse
  caminho como `CMAKE_PREFIX_PATH`.
- O SDK do WebView2 (`WebView2.h`, usado em `libs/kiplatform/port/wxmsw/webview.cpp`)
  não existe no conda-forge. Os headers foram copiados do pacote NuGet oficial
  `microsoft.web.webview2` 1.0.4258.31 (`build\native\include`) para
  `Library\include` do ambiente. Se o ambiente for recriado, repita esse passo.
- O compilador não vem do conda: é o MSVC do Visual Studio Build Tools 2022
  (`C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools`). Não misture
  com o MinGW de `D:\Tools\WinLibs`, que está no PATH: as bibliotecas do
  conda-forge não são compatíveis com ele.

## Configurar e compilar

Rodar em `cmd` (não PowerShell), carregando primeiro o MSVC e depois o ambiente:

```bat
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
call "C:\Users\mr650\miniconda3\condabin\conda.bat" activate kicad
set LIB_ROOT=%CONDA_PREFIX%\Library
cmake -S . -B build\conda-release -G Ninja ^
  -DCMAKE_BUILD_TYPE=RelWithDebInfo ^
  -DCMAKE_C_COMPILER=cl -DCMAKE_CXX_COMPILER=cl ^
  -DCMAKE_PREFIX_PATH="%LIB_ROOT%" ^
  -DCMAKE_INSTALL_PREFIX=build\install\conda-release ^
  -DOCC_INCLUDE_DIR="%LIB_ROOT%\include\opencascade" ^
  -DOCC_LIBRARY_DIR="%LIB_ROOT%\lib" ^
  -DwxWidgets_ROOT_DIR="%LIB_ROOT%" ^
  -DwxWidgets_LIB_DIR="%LIB_ROOT%\lib\vc_x64_dll" ^
  -DPython3_EXECUTABLE="%CONDA_PREFIX%\python.exe" ^
  -DPYTHON_EXECUTABLE="%CONDA_PREFIX%\python.exe" ^
  -DKICAD_BUILD_QA_TESTS=OFF
cmake --build build\conda-release -- -j 6
```

- Use `-j 6`: este Lenovo tem 14 GB de RAM, e com o paralelismo padrão do ninja
  (18 jobs) o MSVC fica sem memória nos headers pré-compilados (erros C3859/C1076).

- O FindOCC do KiCad não acha o OpenCASCADE do conda sozinho; por isso os dois
  `OCC_*` acima são obrigatórios.
- Para os testes, troque `KICAD_BUILD_QA_TESTS` para `ON` (o Boost
  unit_test_framework já está no ambiente).
## Rodar

Os executáveis da pasta de build não abrem direto, porque as DLLs do próprio KiCad
ficam espalhadas pelas subpastas (erro 0xC0000135). Instale e copie as DLLs do
conda para o `bin` da instalação, como faz o instalador oficial:

```powershell
cmake --install build\conda-release
Get-ChildItem C:\Users\mr650\miniconda3\envs\kicad\Library\bin -Filter *.dll |
  Where-Object { -not (Test-Path "build\install\conda-release\bin\$($_.Name)") } |
  Copy-Item -Destination build\install\conda-release\bin
build\install\conda-release\bin\kicad.exe
```

- A cópia é obrigatória, e não basta ativar o ambiente: o preview handler de
  modelos 3D (`kicad_3d_model_previewer.dll`) é carregado com
  `LOAD_LIBRARY_SEARCH_DEFAULT_DIRS`, que ignora o PATH, e também roda dentro do
  Explorer. Sem a cópia aparece "Unable to load the Windows preview handler
  (error 126)". Repita a cópia depois de atualizar pacotes do ambiente.

`kicad-cli.exe version --format about` serve de teste rápido.
