package server

import (
	"fmt"
	"net/http"

	"github.com/gorilla/mux"

	"github.com/replicate/cog/pkg/console"
	"github.com/replicate/cog/pkg/database"
	"github.com/replicate/cog/pkg/docker"
	"github.com/replicate/cog/pkg/serving"
	"github.com/replicate/cog/pkg/storage"
)

// TODO(andreas): decouple saving zip files from image building into two separate API calls?
// TODO(andreas): separate targets for different CUDA versions? how does that change the yaml design?

const topLevelSourceDir = "source"

type Server struct {
	port               int
	db                 database.Database
	dockerImageBuilder docker.ImageBuilder
	servingPlatform    serving.Platform
	store              storage.Storage
}

func NewServer(port int, db database.Database, dockerImageBuilder docker.ImageBuilder, servingPlatform serving.Platform, store storage.Storage) *Server {
	return &Server{
		port:               port,
		db:                 db,
		dockerImageBuilder: dockerImageBuilder,
		servingPlatform:    servingPlatform,
		store:              store,
	}
}

func (s *Server) Start() error {
	router := mux.NewRouter()
	router.Path("/ping").
		Methods(http.MethodGet).
		HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			console.Info("Received ping request")
			w.Write([]byte("pong"))
		})
	router.Path("/v1/repos/{user}/{name}/models/{id}.zip").
		Methods(http.MethodGet).
		HandlerFunc(s.DownloadModel)
	router.Path("/v1/repos/{user}/{name}/models/").
		Methods(http.MethodPut).
		HandlerFunc(s.ReceiveFile)
	router.Path("/v1/repos/{user}/{name}/models/").
		Methods(http.MethodGet).
		HandlerFunc(s.ListModels)
	router.Path("/v1/repos/{user}/{name}/models/{id}").
		Methods(http.MethodGet).
		HandlerFunc(s.SendModelMetadata)
	router.Path("/v1/repos/{user}/{name}/models/{id}").
		Methods(http.MethodDelete).
		HandlerFunc(s.DeleteModel)
	fmt.Println("Starting")
	return http.ListenAndServe(fmt.Sprintf(":%d", s.port), router)
}

func getRepoVars(r *http.Request) (user string, name string, id string) {
	vars := mux.Vars(r)
	return vars["user"], vars["name"], vars["id"]
}
